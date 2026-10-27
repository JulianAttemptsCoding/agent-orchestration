"""Orchestration: one primary agent, deterministic verification, cheap targeted repair.

Loop per task:
  scout -> primary agent -> snapshot diff -> quality gate + project checks
        -> (on failure) resume the SAME session with concrete findings (prompt-cache friendly)
        -> optional read-only reviewer in a disposable export of the result
The second model is never the default; deterministic checks run first because they cost no tokens.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from . import gitsnap
from .checks import CheckResult, render_failures, run_checks
from .config import Config, load_config
from .ledger import append_history, recent_history, render_history, summarize_for_history
from .procs import remove_tree
from .prompts import parse_verdict, primary_prompt, repair_prompt, review_prompt
from .providers import AgentOptions, AgentRun, ProviderError, available, resolve_provider, run_agent
from .quality import GateResult, evaluate_diff, render_findings
from .scout import scout

RISK_TERMS = re.compile(r"\b(auth(?:entication|orization)?|permission|security|password|secret|token|"
                        r"payment|billing|migration|database|data loss|encryption|crypto|concurrency|"
                        r"race condition|production|delete|deletion)\b", re.I)
REVIEW_WHEN = ("never", "risk", "always")
SUBQ_RISK_THRESHOLD = 0.75
N = TypeVar("N", int, float)


@dataclass(frozen=True)
class Plan:
    task: str
    repo: str
    provider: str
    reviewer: str | None
    review_when: str
    review_reason: str
    scout_mode: str
    scout_status: str
    suggested_files: list[str]
    subq_input_tokens: int | None
    scout_seconds: float
    warning: str
    prompt: str
    checks: dict[str, str | None] = field(default_factory=dict)
    risk_probability: float | None = None
    config: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunSettings:
    max_turns: int = 40
    timeout: int = 1800
    custom_command: str | None = None
    max_observed_tokens: int | None = None
    model: str | None = None
    reviewer_model: str | None = None
    lean: bool | None = None
    max_budget_usd: float | None = None
    gate: str | None = None
    repair_rounds: int | None = None


def _pick_reviewer(reviewer: str, main: str) -> str:
    if reviewer != "auto":
        return reviewer
    if main in ("claude", "codex"):
        alt = "codex" if main == "claude" else "claude"
        return alt if available(alt) else main
    return "claude" if available("claude") else "codex"


def _conventions(repo: Path, cfg: Config) -> str:
    if not cfg.conventions_file:
        return ""
    path = (repo / cfg.conventions_file).resolve()
    if not path.is_relative_to(repo.resolve()) or not path.is_file():
        raise ValueError(f"conventions_file not found inside repo: {cfg.conventions_file}")
    return path.read_text(encoding="utf-8", errors="replace")


def plan_task(task: str, repo: Path, *, provider: str = "auto", reviewer: str = "auto",
              review_when: str = "never", mode: str = "assisted", top_k: int = 8,
              max_candidates: int = 32, offline: bool = False, share_code: bool = False,
              config: Config | None = None, test_command: str | None = None) -> Plan:
    if not repo.is_dir():
        raise ValueError("Repo directory not found")
    if not task.strip():
        raise ValueError("Task cannot be empty")
    if mode not in {"baseline", "assisted"}:
        raise ValueError("mode must be baseline or assisted")
    if review_when not in REVIEW_WHEN:
        raise ValueError("review_when must be never, risk or always")
    cfg = config or load_config(repo)
    if test_command is not None:
        cfg = replace(cfg, test_command=test_command or None)
    checks = {"test": cfg.test_command, "lint": cfg.lint_command}
    main = resolve_provider(provider)
    selected = None if review_when == "never" else _pick_reviewer(reviewer, main)
    reason = {"never": "", "always": "review_when=always"}.get(review_when, "")
    risky = RISK_TERMS.search(task)
    if review_when == "risk" and risky:
        reason = f"risk term in task: {risky.group(0)}"
    base: dict[str, Any] = dict(task=task, repo=str(repo.resolve()), provider=main, reviewer=selected,
                review_when=review_when, checks=checks, config=cfg.to_json())
    if mode == "baseline":
        # Baseline = the bare task; no scout, history or quality contract (fair A/B arm).
        return Plan(**base, review_reason=reason, scout_mode="none", scout_status="baseline",
                    suggested_files=[], subq_input_tokens=0, scout_seconds=0.0, warning="", prompt=task)
    result = scout(task, repo, top_k=top_k, max_candidates=max_candidates,
                   offline=offline, share_code=share_code)
    if (review_when == "risk" and not reason and result.risk_probability is not None
            and result.risk_probability >= SUBQ_RISK_THRESHOLD):
        reason = f"SubQ review_risk={result.risk_probability:.2f}"
    paths = [entry.path for entry in result.files]
    prompt = primary_prompt(task, files=paths, checks=[c for c in checks.values() if c],
                            conventions=_conventions(repo, cfg),
                            history=render_history(recent_history(repo, cfg.history_in_prompt)))
    return Plan(**base, review_reason=reason, scout_mode=result.mode, scout_status=result.status,
                suggested_files=paths, subq_input_tokens=result.input_tokens,
                scout_seconds=result.elapsed_seconds, warning=result.warning, prompt=prompt,
                risk_probability=result.risk_probability)


def per_run_usage(runs: list[AgentRun]) -> list[AgentRun]:
    """Convert cumulative (resumed Claude) usage into per-invocation deltas."""
    out: list[AgentRun] = []
    last_by_session: dict[str, AgentRun] = {}
    for r in runs:
        prev = last_by_session.get(r.session_id or "")
        if r.usage_cumulative and prev is not None:
            fields = ("input_tokens", "output_tokens", "cached_input_tokens", "cost_usd")
            cur = [getattr(r, f) for f in fields]
            old = [getattr(prev, f) for f in fields]
            if all(c is None or (o is not None and c >= o) for c, o in zip(cur, old, strict=True)):
                delta: dict[str, Any] = {f: (c - o if c is not None and o is not None else None)
                         for f, c, o in zip(fields, cur, old, strict=True)}
                out.append(replace(r, **delta, usage_cumulative=False))
            else:
                out.append(replace(r, usage_cumulative=False))
        else:
            out.append(r)
        if r.session_id:
            last_by_session[r.session_id] = r  # keep the raw cumulative value
    return out


def sum_known(values: Iterable[N | None]) -> N | None:
    """Sum, or None if any value is unknown. Unknown usage is never treated as zero."""
    items = list(values)
    if any(v is None for v in items):
        return None
    return sum(v for v in items if v is not None)


def total_tokens(runs: list[AgentRun], subq_input: int | None) -> dict:
    runs = per_run_usage(runs)
    known_in = sum_known([subq_input, *(r.input_tokens for r in runs)])
    known_out = sum_known(r.output_tokens for r in runs)
    cost = sum_known(r.cost_usd for r in runs) if runs else None
    return {
        "total_tokens": sum_known([known_in, known_out]),
        "input_tokens": known_in,
        "output_tokens": known_out,
        "cached_input_tokens": sum_known(r.cached_input_tokens for r in runs),
        "subq_input_tokens": subq_input,
        "agent_cost_usd": round(cost, 6) if cost is not None else None,
        "complete_usage": known_in is not None and known_out is not None,
    }


def _known_total(runs: list[AgentRun], subq: int | None) -> int | None:
    return total_tokens(runs, subq)["total_tokens"]


@dataclass
class _Verification:
    gate: GateResult | None
    checks: list[CheckResult]
    before: str | None
    after: str | None
    note: str = ""

    def passed(self, gate_mode: str) -> bool:
        gate_ok = self.gate is None or gate_mode != "block" or not self.gate.blocked
        return gate_ok and all(c.passed for c in self.checks)


def _verify(repo: Path, before: str | None, plan: Plan, cfg: Config, gate_mode: str) -> _Verification:
    checks = run_checks(plan.checks, repo, cfg.check_timeout)
    if before is None:
        return _Verification(None, checks, None, None, "diff gate skipped: not a git repository")
    after = gitsnap.snapshot(repo)
    if gate_mode == "off":
        return _Verification(None, checks, before, after)
    diff_text = gitsnap.diff(repo, before, after)
    gate = evaluate_diff(repo, diff_text, test_globs=cfg.test_globs, allowed_paths=cfg.allowed_paths,
                         max_changed_lines=cfg.max_changed_lines,
                         duplicate_min_lines=cfg.duplicate_min_lines,
                         disabled_rules=frozenset(cfg.disabled_rules), exclude_paths=cfg.exclude_paths)
    return _Verification(gate, checks, before, after)


def _review(plan: Plan, repo: Path, v: _Verification, settings: RunSettings, lean: bool) -> AgentRun:
    assert plan.reviewer and v.before and v.after
    tmp = Path(tempfile.mkdtemp(prefix="contextweft-review-"))
    try:
        # Read-only is enforced by construction: the reviewer only ever sees a throwaway copy.
        gitsnap.export_tree(repo, v.after, tmp)
        checks = "; ".join(f"{c.command}: {'pass' if c.passed else 'FAIL'}" for c in v.checks)
        prompt = review_prompt(plan.task, diff=gitsnap.diff(repo, v.before, v.after, context=3),
                               gate=render_findings(v.gate.findings) if v.gate else "", checks=checks)
        opts = AgentOptions(review=True, max_turns=min(settings.max_turns, 15), timeout=settings.timeout,
                            model=settings.reviewer_model, lean=lean,
                            custom_command=settings.custom_command)
        return run_agent(plan.reviewer, prompt, repo=tmp, opts=opts)
    finally:
        remove_tree(tmp)


def execute(plan: Plan, *, dry_run: bool = False, settings: RunSettings | None = None,
            record_history: bool = True) -> dict:
    s = settings or RunSettings()
    if s.max_observed_tokens is not None and s.max_observed_tokens < 1:
        raise ValueError("max_observed_tokens must be positive")
    cfg = Config(**plan.config).validate() if plan.config else Config()
    gate_mode = s.gate or cfg.gate
    rounds_allowed = cfg.repair_rounds if s.repair_rounds is None else s.repair_rounds
    lean = cfg.lean if s.lean is None else s.lean
    if dry_run:
        return {"dry_run": True, "plan": plan.to_json(), "runs": [],
                "totals": total_tokens([], plan.subq_input_tokens)}
    repo = Path(plan.repo)
    start = time.monotonic()
    before = gitsnap.snapshot(repo) if gitsnap.is_git_repo(repo) else None
    check_cmds = [c for c in plan.checks.values() if c]
    allowed = tuple(cfg.allowed_tools) + tuple(t for c in check_cmds for t in (f"Bash({c})", f"Bash({c} *)"))
    opts = AgentOptions(max_turns=s.max_turns, timeout=s.timeout, model=s.model or cfg.model, lean=lean,
                        allowed_tools=allowed, max_budget_usd=s.max_budget_usd or cfg.max_budget_usd,
                        custom_command=s.custom_command)
    runs: list[AgentRun] = []
    notes: list[str] = []
    prompt = plan.prompt
    verification: _Verification | None = None
    repairs = 0

    def over_budget() -> bool:
        if s.max_observed_tokens is None:
            return False
        known = _known_total(runs, plan.subq_input_tokens)
        return known is None or known >= s.max_observed_tokens

    def attempt(p: str, o: AgentOptions) -> bool:
        nonlocal verification
        run = run_agent(plan.provider, p, repo=repo, opts=o)
        runs.append(run)
        if not run.success:
            notes.append(f"primary agent failed: {run.error}")
            return False
        verification = _verify(repo, before, plan, cfg, gate_mode)
        if verification.note:
            notes.append(verification.note)
        return verification.passed(gate_mode)

    ok = attempt(prompt, opts)
    while not ok and runs[-1].success and repairs < rounds_allowed and verification is not None:
        if over_budget():
            notes.append("repair skipped: observed token threshold reached or usage unknown")
            break
        repairs += 1
        ok = _repair(plan, runs[-1], verification, attempt, opts, review_text="")

    verdict = None
    repaired_after_review = False
    review_reason = plan.review_reason
    if verification and verification.gate and verification.gate.findings and plan.review_when == "risk" \
            and not review_reason:
        review_reason = "gate warnings remain"
    if plan.reviewer and plan.review_when != "never" and review_reason:
        if not ok:
            notes.append("review skipped: verification already failing")
        elif verification is None or verification.after is None or before is None:
            notes.append("review skipped: requires a git repository for an isolated snapshot")
        elif over_budget():
            notes.append("review skipped: observed token threshold reached or usage unknown")
        else:
            review = _review(plan, repo, verification, s, lean)
            runs.append(review)
            verdict = parse_verdict(review.text) if review.success else None
            if not review.success:
                notes.append(f"reviewer failed: {review.error}")
            elif verdict == "FAIL" and repairs < rounds_allowed and not over_budget():
                repairs += 1
                primary = next(r for r in reversed(runs) if r.role == "primary")
                # Not re-reviewed (cost cap); the repaired result must pass gate + checks again.
                ok = _repair(plan, primary, verification, attempt, opts, review_text=review.text)
                repaired_after_review = True
            elif verdict is None:
                notes.append("reviewer gave no VERDICT line")

    primary_runs = [r for r in runs if r.role == "primary"]
    final_checks = verification.checks if verification else []
    verified = ok and bool(final_checks) and all(c.passed for c in final_checks)
    review_blocks = verdict == "FAIL" and not repaired_after_review
    success = bool(primary_runs) and ok and primary_runs[-1].success and not review_blocks
    status = ("verified" if success and verified else "passed-unverified" if success
              else "agent-failed" if primary_runs and not primary_runs[-1].success else "failed-verification")
    changed = sorted({p for p in _changed_files(repo, before, verification)})
    if record_history and success:
        append_history(repo, {"task": plan.task[:300], "files": changed[:20], "verified": verified,
                              "summary": summarize_for_history(primary_runs[-1].text)})
    return {
        "dry_run": False,
        "status": status,
        "success": success,
        "verified": verified,
        "plan": {k: v for k, v in plan.to_json().items() if k not in ("prompt", "config")},
        "runs": [r.to_json() for r in per_run_usage(runs)],
        "repair_rounds": repairs,
        "gate": verification.gate.to_json() if verification and verification.gate else None,
        "checks": [c.to_json() for c in final_checks],
        "review": {"reviewer": plan.reviewer if verdict or any(r.role == "review" for r in runs) else None,
                   "reason": review_reason, "verdict": verdict,
                   "repaired_after_review": repaired_after_review},
        "changed_files": changed,
        "totals": total_tokens(runs, plan.subq_input_tokens),
        "elapsed_seconds": round(time.monotonic() - start + plan.scout_seconds, 3),
        "notes": notes,
    }


def _changed_files(repo: Path, before: str | None, v: _Verification | None) -> list[str]:
    if before is None or v is None or v.after is None:
        return []
    return [p for _, p in gitsnap.changed_files(repo, before, v.after)]


def _repair(plan: Plan, last: AgentRun, v: _Verification, attempt: Callable[[str, AgentOptions], bool],
            opts: AgentOptions, *, review_text: str) -> bool:
    resumable = last.provider in ("claude", "codex") and last.session_id
    gate_text = render_findings(v.gate.findings) if v.gate and v.gate.findings else ""
    p = repair_prompt(plan.task, gate=gate_text, check_failures=render_failures(v.checks),
                      review=review_text[-3000:], resumed=bool(resumable))
    return attempt(p, replace(opts, resume=last.session_id if resumable else None))


def write_record(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def sanitize_record(result: dict, case_id: str) -> dict:
    """Benchmark telemetry: no model prose, no check output, no stderr."""
    row = {"case_id": case_id, **{k: v for k, v in result.items() if k not in ("runs", "checks")}}
    row["runs"] = [{k: v for k, v in r.items() if k not in ("text", "error_detail")} for r in result.get("runs", [])]
    row["checks"] = [{k: v for k, v in c.items() if k != "output_tail"} for c in result.get("checks", [])]
    return row


def run_batch(tasks: list[str], repo: Path, *, plan_kwargs: dict, settings: RunSettings,
              stop_on_failure: bool = True) -> dict:
    """Sequential tasks, each in a FRESH agent session, linked by verified history."""
    results = []
    for i, task in enumerate(tasks, 1):
        plan = plan_task(task, repo, **plan_kwargs)
        try:
            res = execute(plan, settings=settings)
        except ProviderError as exc:
            res = {"status": "agent-failed", "success": False, "notes": [str(exc)], "totals": {}}
        results.append({"index": i, "task": task, "status": res["status"], "success": res["success"],
                        "changed_files": res.get("changed_files", []), "totals": res.get("totals", {}),
                        "notes": res.get("notes", [])})
        if not res["success"] and stop_on_failure:
            break
    return {"tasks": len(tasks), "attempted": len(results),
            "succeeded": sum(r["success"] for r in results),
            "total_tokens": sum_known(r["totals"].get("total_tokens") for r in results) if results else None,
            "results": results}
