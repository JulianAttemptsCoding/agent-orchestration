"""Installable CLI. No automatic GitHub publishing, commits or pushes."""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import __version__, gitsnap
from .bench import run_bench
from .config import CONFIG_NAME, Config, load_config, parse_config, render_template
from .engine import RunSettings, execute, plan_task, run_batch, sanitize_record, write_record
from .evaluation import evaluate
from .metrics import compare_logs
from .providers import ProviderError
from .quality import evaluate_diff, render_findings
from .scout import scout

HOOK_SNIPPET = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": "contextweft gate --hook claude"}]}]}}


def install_skills(repo: Path, *, force: bool = False) -> list[str]:
    if not repo.is_dir():
        raise ValueError("Repository directory does not exist")
    content = resources.files("contextweft").joinpath("SKILL.md").read_text(encoding="utf-8")
    paths = [repo / ".claude/skills/contextweft/SKILL.md", repo / ".agents/skills/contextweft/SKILL.md"]
    existing = [str(p) for p in paths if p.exists()]
    if existing and not force:
        raise ValueError("Skill already exists; use --force to overwrite: " + ", ".join(existing))
    for dest in paths:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return [str(p) for p in paths]


def init_config(repo: Path, *, force: bool = False) -> str:
    if not repo.is_dir():
        raise ValueError("Repository directory does not exist")
    path = repo / CONFIG_NAME
    if path.exists() and not force:
        raise ValueError(f"{path} exists; use --force to overwrite")
    path.write_text(render_template(repo), encoding="utf-8")
    load_config(repo)  # template must always parse
    return str(path)


def _version(cmd: str) -> str | None:
    from .procs import resolve_executable
    exe = resolve_executable(cmd)
    if not exe:
        return None
    try:
        cp = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown (version check failed)"
    return (cp.stdout or cp.stderr).strip().splitlines()[0][:80] if (cp.stdout or cp.stderr).strip() else "unknown"


def doctor(repo: Path) -> dict:
    report: dict = {
        "contextweft": __version__,
        "python": sys.version.split()[0],
        "git": _version("git"),
        "claude_cli": _version("claude"),
        "codex_cli": _version("codex"),
        "subq_key_set": bool(os.environ.get("SUBQ_API_KEY")),
        "repo": str(repo.resolve()),
        "repo_is_git": gitsnap.is_git_repo(repo),
        "note": "Checks local tools only: no credentials are validated and no inference is run.",
    }
    try:
        cfg = load_config(repo)
        report["config"] = "found" if (repo / CONFIG_NAME).is_file() else "defaults (run `contextweft init`)"
        report["test_command"] = cfg.test_command
    except ValueError as exc:
        report["config"] = f"invalid: {exc}"
    problems = []
    if not report["git"]:
        problems.append("git not found: diff gate, review isolation and bench need it")
    if not (report["claude_cli"] or report["codex_cli"]):
        problems.append("neither claude nor codex CLI on PATH")
    if not report["repo_is_git"]:
        problems.append("repo is not a git repository: diff gate disabled")
    if str(report.get("config", "")).startswith("invalid"):
        problems.append("contextweft.toml invalid")
    if not report.get("test_command"):
        problems.append("no [checks] test command: results will be 'passed-unverified'")
    report["problems"] = problems
    return report


def gate_command(repo: Path, base: str, hook: str | None) -> tuple[dict, int]:
    payload: object = {}
    if hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            repo = Path(payload["cwd"])
        if not gitsnap.is_git_repo(repo):
            return {"skipped": "not a git repository"}, 0
    base_tree = gitsnap.tree_of(repo, base)
    # Rules come from the base commit, so uncommitted (possibly agent-made) config edits
    # can't loosen the gate that judges them. Outside hooks, an uncommitted config is used.
    committed = gitsnap.show_file(repo, base_tree, CONFIG_NAME)
    if committed is not None:
        cfg, source = parse_config(committed, f"{base}:{CONFIG_NAME}"), f"{base}:{CONFIG_NAME}"
    elif hook:
        cfg, source = Config(), "defaults (config not committed)"
    else:
        cfg, source = load_config(repo), "working tree" if (repo / CONFIG_NAME).is_file() else "defaults"
    after = gitsnap.snapshot(repo)
    result = evaluate_diff(repo, gitsnap.diff(repo, base_tree, after), test_globs=cfg.test_globs,
                           allowed_paths=cfg.allowed_paths, max_changed_lines=cfg.max_changed_lines,
                           duplicate_min_lines=cfg.duplicate_min_lines,
                           disabled_rules=frozenset(cfg.disabled_rules), exclude_paths=cfg.exclude_paths)
    out = {**result.to_json(), "config_source": source}
    if hook == "claude":
        # Stop hook: exit 2 feeds stderr back to Claude and keeps it working. Only once per
        # stop (stop_hook_active) so a stubborn finding can never loop forever.
        if result.blocked and not (isinstance(payload, dict) and payload.get("stop_hook_active")):
            print("ContextWeft quality gate blocked completion. Fix these, then finish:\n"
                  + render_findings(result.findings), file=sys.stderr)
            return out, 2
        return out, 0
    return out, 1 if result.blocked else 0


def _add_scout_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--repo", type=Path, default=Path.cwd())
    s.add_argument("--offline", action="store_true", help="Never call SubQ (local ranking only)")
    s.add_argument("--share-code", action="store_true", help="Send bounded file excerpts to SubQ (opt-in)")
    s.add_argument("--top-k", type=int, default=8)
    s.add_argument("--max-candidates", type=int, default=32)


def _add_run_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--provider", choices=("auto", "claude", "codex", "custom"), default="auto")
    s.add_argument("--reviewer", choices=("auto", "claude", "codex", "custom"), default="auto")
    s.add_argument("--review-when", choices=("never", "risk", "always"), default="never",
                   help="risk = risky task terms, SubQ risk >= 0.75, or leftover gate warnings")
    s.add_argument("--mode", choices=("assisted", "baseline"), default="assisted")
    s.add_argument("--test", dest="test_command", help="Override [checks] test command ('' disables)")
    s.add_argument("--max-turns", type=int, default=40)
    s.add_argument("--timeout", type=int, default=1800, help="Seconds per agent invocation")
    s.add_argument("--model")
    s.add_argument("--reviewer-model")
    s.add_argument("--max-budget-usd", type=float, help="Claude only: client-side spend cap per invocation")
    s.add_argument("--max-observed-tokens", type=int,
                   help="Stop optional repair/review once known tokens reach this (unknown usage also stops)")
    s.add_argument("--gate", choices=("off", "warn", "block"))
    s.add_argument("--repair-rounds", type=int)
    s.add_argument("--no-lean", action="store_true", help="Load MCP servers and skills in agent runs")
    s.add_argument("--custom-command", help="Command reading prompt on stdin, printing JSON (see docs)")


def _settings(args: argparse.Namespace) -> RunSettings:
    return RunSettings(max_turns=args.max_turns, timeout=args.timeout, custom_command=args.custom_command,
                       max_observed_tokens=args.max_observed_tokens, model=args.model,
                       reviewer_model=args.reviewer_model, lean=False if args.no_lean else None,
                       max_budget_usd=args.max_budget_usd, gate=args.gate, repair_rounds=args.repair_rounds)


def _plan_kwargs(args: argparse.Namespace) -> dict:
    return dict(provider=args.provider, reviewer=args.reviewer, review_when=args.review_when,
                mode=args.mode, top_k=args.top_k, max_candidates=args.max_candidates,
                offline=args.offline, share_code=args.share_code, test_command=args.test_command)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="contextweft", description="Token-aware, quality-gated coding-agent orchestration")
    p.add_argument("--version", action="version", version=f"contextweft {__version__}")
    cmd = p.add_subparsers(dest="command", required=True)
    d = cmd.add_parser("doctor", help="Check local tools and repo setup (no inference)")
    d.add_argument("--repo", type=Path, default=Path.cwd())
    n = cmd.add_parser("init", help=f"Write a {CONFIG_NAME} template (auto-detects test command)")
    n.add_argument("--repo", type=Path, default=Path.cwd())
    n.add_argument("--force", action="store_true")
    i = cmd.add_parser("install", help="Install advisory skills for Claude Code and Codex")
    i.add_argument("--repo", type=Path, default=Path.cwd())
    i.add_argument("--force", action="store_true")
    g = cmd.add_parser("gate", help="Run the diff quality gate on uncommitted work (no tokens)")
    g.add_argument("--repo", type=Path, default=Path.cwd())
    g.add_argument("--base", default="HEAD", help="Compare the working tree against this ref")
    g.add_argument("--hook", choices=("claude",), help="Run as a Claude Code Stop hook")
    sc = cmd.add_parser("scout", help="Rank likely-relevant files (advisory)")
    sc.add_argument("task")
    _add_scout_args(sc)
    for name, help_text in (("plan", "Show the prompt/plan without running an agent"),
                            ("run", "Run one task through the agent + verification loop")):
        s = cmd.add_parser(name, help=help_text)
        s.add_argument("task")
        _add_scout_args(s)
        _add_run_args(s)
        if name == "run":
            s.add_argument("--dry-run", action="store_true", help="No coding agent call")
            s.add_argument("--case-id", help="Identifier for an A/B test task")
            s.add_argument("--record", type=Path, help="Append sanitized run record to JSONL")
    b = cmd.add_parser("batch", help="Run tasks from a file sequentially, fresh session per task")
    b.add_argument("tasks_file", type=Path, help="One task per line (# comments and '- [ ]' allowed)")
    b.add_argument("--keep-going", action="store_true", help="Continue after a failed task")
    _add_scout_args(b)
    _add_run_args(b)
    e = cmd.add_parser("eval", help="Evaluate scout file recall on a gold dataset")
    e.add_argument("dataset", type=Path)
    e.add_argument("--repo", type=Path, default=Path.cwd())
    e.add_argument("--offline", action="store_true")
    e.add_argument("--top-k", type=int, default=8)
    e.add_argument("--max-candidates", type=int, default=32)
    bn = cmd.add_parser("bench", help="Paired baseline-vs-assisted benchmark in fresh clones")
    bn.add_argument("cases", type=Path, help='JSONL: {"id","task","test"[,"ref"]}')
    bn.add_argument("--repo", type=Path, default=Path.cwd())
    bn.add_argument("--out", type=Path, required=True)
    bn.add_argument("--ref", default="HEAD")
    bn.add_argument("--keep", action="store_true", help="Keep clones for inspection")
    bn.add_argument("--online", action="store_true", help="Allow SubQ calls in the assisted arm")
    _add_run_args(bn)
    c = cmd.add_parser("compare", help="Compare recorded paired baseline and assisted JSONL")
    c.add_argument("--baseline", required=True, type=Path)
    c.add_argument("--assisted", required=True, type=Path)
    return p


def read_tasks(path: Path) -> list[str]:
    tasks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or t.startswith("- [x]"):
            continue
        for prefix in ("- [ ]", "-", "*"):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
                break
        if t:
            tasks.append(t)
    if not tasks:
        raise ValueError(f"No tasks found in {path}")
    return tasks


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")  # human text must never crash the run
    args = parser().parse_args(argv)
    code = 0
    try:
        if args.command == "doctor":
            result = doctor(args.repo)
        elif args.command == "init":
            result = {"config": init_config(args.repo, force=args.force),
                      "next": "Review the test command, then: contextweft run 'TASK'"}
        elif args.command == "install":
            result = {"installed_skills": install_skills(args.repo, force=args.force),
                      "optional_claude_stop_hook": HOOK_SNIPPET}
        elif args.command == "gate":
            result, code = gate_command(args.repo, args.base, args.hook)
            if args.hook:
                return code  # hooks: keep stdout clean
        elif args.command == "scout":
            result = scout(args.task, args.repo, offline=args.offline, share_code=args.share_code,
                           top_k=args.top_k, max_candidates=args.max_candidates).to_json()
        elif args.command == "eval":
            result = evaluate(args.repo, args.dataset, offline=args.offline,
                              top_k=args.top_k, max_candidates=args.max_candidates)
        elif args.command == "compare":
            result = compare_logs(args.baseline, args.assisted)
        elif args.command == "bench":
            result = run_bench(args.repo, args.cases, args.out, provider=args.provider, ref=args.ref,
                               settings=_settings(args), offline=not args.online, keep=args.keep)
        elif args.command == "batch":
            result = run_batch(read_tasks(args.tasks_file), args.repo, plan_kwargs=_plan_kwargs(args),
                               settings=_settings(args), stop_on_failure=not args.keep_going)
            code = 0 if result["succeeded"] == result["tasks"] else 1
        else:
            if getattr(args, "record", None) and not args.case_id:
                raise ValueError("--record requires --case-id to support paired comparisons")
            plan = plan_task(args.task, args.repo, **_plan_kwargs(args))
            if args.command == "plan":
                result = plan.to_json()
            else:
                result = execute(plan, dry_run=args.dry_run, settings=_settings(args))
                if args.record and not args.dry_run:
                    write_record(args.record, sanitize_record(result, args.case_id))
                if not args.dry_run and not result["success"]:
                    code = 1
        # ASCII-escaped: agent text can hold emoji, and a redirected Windows stdout is cp1252.
        print(json.dumps(result, indent=2))
        return code
    except (ValueError, OSError, ProviderError, gitsnap.GitError) as exc:
        print(f"contextweft: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
