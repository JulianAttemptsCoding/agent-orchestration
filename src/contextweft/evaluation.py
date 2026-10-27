"""Ground-truth file-retrieval evaluation (recall@k of the scout)."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from .scout import scout


def jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if line.strip():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}:{line_number}: expected JSON object")
                rows.append(obj)
    return rows


def evaluate(repo: Path, dataset: Path, *, offline: bool = False,
             share_code: bool = False, top_k: int = 12, max_candidates: int = 40) -> dict:
    cases = jsonl(dataset)
    if not cases:
        raise ValueError("Dataset has no cases")
    report = []
    for case in cases:
        if not isinstance(case.get("task"), str) or not isinstance(case.get("gold_files"), list) or not case["gold_files"]:
            raise ValueError("Each case needs a task and nonempty gold_files array")
        if not all(isinstance(g, str) for g in case["gold_files"]):
            raise ValueError("gold_files must be strings")
        result = scout(case["task"], repo, offline=offline, share_code=share_code,
                       top_k=top_k, max_candidates=max_candidates)
        gold = set(case["gold_files"])
        selected = {f.path for f in result.files}
        recall = len(gold & selected) / len(gold)
        report.append({"id": case.get("id", case["task"]), "recall_at_k": recall,
                       "all_gold_found": gold <= selected, "missed": sorted(gold - selected),
                       "status": result.status, "subq_input_tokens": result.input_tokens,
                       "elapsed_seconds": result.elapsed_seconds})
    return {"cases": len(report), "top_k": top_k,
            "mean_recall_at_k": round(statistics.mean(x["recall_at_k"] for x in report), 4),
            "all_gold_found_rate": round(statistics.mean(x["all_gold_found"] for x in report), 4),
            "subq_tokens": (sum(x["subq_input_tokens"] for x in report)
                            if all(x["subq_input_tokens"] is not None for x in report) else None),
            "details": report}

