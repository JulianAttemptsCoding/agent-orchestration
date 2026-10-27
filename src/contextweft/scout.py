"""A non-blocking, conservative ranking layer."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .repository import local_candidates
from .subq import SubQError, judge


@dataclass(frozen=True)
class SelectedFile:
    path: str
    local_score: float
    subq_probability: float | None


@dataclass(frozen=True)
class ScoutResult:
    task: str
    mode: str
    status: str
    files: list[SelectedFile]
    candidate_count: int
    input_tokens: int | None
    elapsed_seconds: float
    warning: str
    risk_probability: float | None = None

    def to_json(self) -> dict:
        return asdict(self)


def scout(task: str, repo: Path, *, top_k: int = 12, max_candidates: int = 40,
          offline: bool = False, share_code: bool = False, timeout: float = 12.0) -> ScoutResult:
    if not task.strip():
        raise ValueError("Task must not be empty")
    if not (1 <= top_k <= 128 and top_k <= max_candidates <= 128):
        raise ValueError("Require 1 <= top_k <= max_candidates <= 128")
    if not repo.is_dir():
        raise ValueError("Repo directory not found")
    start = time.monotonic()
    candidates = local_candidates(repo.resolve(), task, max_candidates=max_candidates)
    scores: dict[str, float] = {}
    tokens: int | None = 0
    status = "offline"
    warning = ""
    risk_probability = None
    if not offline and candidates:
        try:
            dec = judge(task, candidates, include_code=share_code, timeout=timeout)
            scores, tokens = dec.probabilities, dec.input_tokens
            risk_probability = dec.risk_probability
            status = "subq"
        except SubQError as exc:
            status = "fallback"
            warning = f"SubQ unavailable: {exc}. Local lexical ranking used."
    # Preserve the strongest lexical hits; SubQ can re-rank the remainder.
    # All paths remain open to Claude; no hook can block file access.
    by_path = {c.path: c for c in candidates}
    keep = [c.path for c in candidates[:min(3, top_k)]]
    remaining = [c for c in candidates if c.path not in keep]
    if status == "subq":
        # Use SubQ as the primary ranking signal *after* guaranteed lexical anchors.
        remaining.sort(key=lambda c: (-scores.get(c.path, 0.0), -c.score, c.path))
    selected = (keep + [c.path for c in remaining])[:top_k]
    files = [SelectedFile(p, by_path[p].score, scores.get(p)) for p in selected]
    return ScoutResult(task, "snippets" if share_code else "paths", status, files,
                       len(candidates), tokens, round(time.monotonic() - start, 4), warning, risk_probability)
