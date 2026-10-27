"""Paired A/B comparisons from actual recorded runs, never projected savings."""
from __future__ import annotations

import json
import math
import random
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any

MIN_CASES_FOR_INFERENCE = 10
BOOTSTRAP_SAMPLES = 5000


def _rows(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for n, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON: {path}:{n}") from exc
            key = row.get("case_id") if isinstance(row, dict) else None
            if not isinstance(key, str) or not key or key in rows:
                raise ValueError(f"Each case_id must be unique and nonempty: {path}:{n}")
            rows[key] = row
    return rows


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _totals(row: dict) -> dict:
    t = row.get("totals")
    return t if isinstance(t, dict) else {}


def _total(row: dict) -> float | None:
    t = _totals(row)
    if "total_tokens" in t:
        return _num(t["total_tokens"])
    i, o = _num(t.get("input_tokens")), _num(t.get("output_tokens"))
    return i + o if i is not None and o is not None else None


GETTERS: dict[str, Callable[[dict], float | None]] = {
    "total_tokens": _total,
    "input_tokens": lambda r: _num(_totals(r).get("input_tokens")),
    "output_tokens": lambda r: _num(_totals(r).get("output_tokens")),
    "agent_cost_usd": lambda r: _num(_totals(r).get("agent_cost_usd")),
    "elapsed_seconds": lambda r: _num(r.get("elapsed_seconds")),
}


def bootstrap_mean_ci(diffs: list[float], seed: int = 0, level: float = 0.95) -> tuple[float, float]:
    rng = random.Random(seed)  # noqa: S311 - statistics, not crypto
    n = len(diffs)
    means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(BOOTSTRAP_SAMPLES))
    lo = means[int((1 - level) / 2 * BOOTSTRAP_SAMPLES)]
    hi = means[min(BOOTSTRAP_SAMPLES - 1, int((1 + level) / 2 * BOOTSTRAP_SAMPLES))]
    return lo, hi


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def compare_logs(baseline: Path, assisted: Path) -> dict:
    a, b = _rows(baseline), _rows(assisted)
    if not a or set(a) != set(b):
        raise ValueError("Baseline and assisted logs must have the same nonempty case_id set")
    keys = sorted(a)
    out: dict = {"paired_cases": len(keys), "metrics": {},
                 "warning": "Observed values only. Requires equivalent repo snapshots, same models and "
                            "independent ground-truth tests; small samples prove nothing."}
    for metric, getter in GETTERS.items():
        raw_base = [getter(a[k]) for k in keys]
        raw_assist = [getter(b[k]) for k in keys]
        base = [v for v in raw_base if v is not None]
        assist = [v for v in raw_assist if v is not None]
        if len(base) != len(keys) or len(assist) != len(keys):
            missing = 2 * len(keys) - len(base) - len(assist)
            out["metrics"][metric] = {"status": "unknown_or_missing", "missing_values": missing}
            continue
        ma, mb = statistics.fmean(base), statistics.fmean(assist)
        diffs = [y - x for x, y in zip(base, assist, strict=True)]
        entry: dict[str, Any] = {"baseline_mean": round(ma, 4), "assisted_mean": round(mb, 4),
                 "change_percent": round(100 * (mb - ma) / ma, 2) if ma else None,
                 "median_paired_diff": round(statistics.median(diffs), 4)}
        if len(keys) >= MIN_CASES_FOR_INFERENCE:
            lo, hi = bootstrap_mean_ci(diffs)
            entry["mean_diff_95ci"] = [round(lo, 4), round(hi, 4)]
            entry["significant"] = lo > 0 or hi < 0
        else:
            entry["significant"] = None
        out["metrics"][metric] = entry
    sa = [a[k].get("verified_success") is True for k in keys]
    sb = [b[k].get("verified_success") is True for k in keys]
    if all(isinstance(a[k].get("verified_success"), bool) and isinstance(b[k].get("verified_success"), bool)
           for k in keys):
        only_base = sum(x and not y for x, y in zip(sa, sb, strict=True))
        only_assist = sum(y and not x for x, y in zip(sa, sb, strict=True))
        out["metrics"]["verified_success"] = {
            "baseline_rate": round(sum(sa) / len(keys), 4), "assisted_rate": round(sum(sb) / len(keys), 4),
            "only_baseline_passed": only_base, "only_assisted_passed": only_assist,
            "mcnemar_p": round(mcnemar_exact(only_base, only_assist), 4),
        }
    else:
        out["metrics"]["verified_success"] = {"status": "unknown_or_missing"}
    if len(keys) < MIN_CASES_FOR_INFERENCE:
        out["inference"] = f"skipped: fewer than {MIN_CASES_FOR_INFERENCE} paired cases"
    return out
