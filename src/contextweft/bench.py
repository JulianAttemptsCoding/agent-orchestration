"""Paired A/B benchmark: plain single agent (baseline) vs ContextWeft loop (assisted).

Every case/arm runs in its own fresh clone at the same ref, with the same provider,
model and limits. Success is decided by a ground-truth command the agent never
sees, run by this harness after the agent exits.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from . import gitsnap
from .checks import run_check
from .engine import RunSettings, execute, plan_task, sanitize_record, write_record
from .evaluation import jsonl
from .metrics import compare_logs
from .procs import remove_tree
from .providers import ProviderError

ARMS = ("baseline", "assisted")


def load_cases(path: Path) -> list[dict]:
    cases = jsonl(path)
    if not cases:
        raise ValueError("Benchmark file has no cases")
    seen = set()
    for c in cases:
        if not isinstance(c.get("id"), str) or not c["id"] or c["id"] in seen:
            raise ValueError("Each case needs a unique string id")
        if not isinstance(c.get("task"), str) or not isinstance(c.get("test"), str):
            raise ValueError(f"Case {c['id']}: needs string task and test (ground-truth command)")
        seen.add(c["id"])
    return cases


def run_bench(repo: Path, cases_path: Path, out_dir: Path, *, provider: str, ref: str = "HEAD",
              settings: RunSettings, offline: bool = True, keep: bool = False,
              ground_truth_timeout: int = 900) -> dict:
    if not gitsnap.is_git_repo(repo):
        raise ValueError("Benchmark repo must be a git repository (cases run in fresh clones)")
    cases = load_cases(cases_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs = {arm: out_dir / f"{arm}.jsonl" for arm in ARMS}
    for p in logs.values():
        if p.exists():
            raise ValueError(f"{p} exists; use a new --out directory so runs are not mixed")
    work = Path(tempfile.mkdtemp(prefix="contextweft-bench-"))
    try:
        for i, case in enumerate(cases):
            # Alternate arm order so time-varying API behaviour doesn't favour one arm.
            for arm in (ARMS if i % 2 == 0 else ARMS[::-1]):
                clone = work / f"{case['id']}-{arm}"
                gitsnap.clone_at(repo, case.get("ref", ref), clone)
                arm_settings = settings if arm == "assisted" else replace(settings, gate="off", repair_rounds=0)
                try:
                    plan = plan_task(case["task"], clone, provider=provider, mode=arm, offline=offline,
                                     review_when=case.get("review_when", "never") if arm == "assisted" else "never")
                    result = execute(plan, settings=arm_settings, record_history=False)
                except ProviderError as exc:
                    result = {"status": "agent-failed", "success": False, "notes": [str(exc)],
                              "totals": {"total_tokens": None}, "runs": [], "checks": []}
                truth = run_check("ground_truth", case["test"], clone, ground_truth_timeout)
                row = sanitize_record(result, case["id"])
                row.update(arm=arm, verified_success=truth.passed,
                           ground_truth={"returncode": truth.returncode, "error": truth.error,
                                         "elapsed_seconds": truth.elapsed_seconds})
                write_record(logs[arm], row)
                if not keep:
                    remove_tree(clone)
    finally:
        if not keep:
            remove_tree(work)
    report = compare_logs(logs["baseline"], logs["assisted"])
    report["logs"] = {k: str(v) for k, v in logs.items()}
    if keep:
        report["workdir"] = str(work)
    return report
