"""Per-repo run history: compact, verified state handed to the next fresh agent session.

Long sessions degrade (context rot); short fresh sessions that inherit a few lines
of *verified* history keep context small without losing the thread.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_DIR = ".contextweft"
HISTORY_FILE = "history.jsonl"
MAX_SUMMARY_CHARS = 280


def state_dir(repo: Path) -> Path:
    d = repo / STATE_DIR
    d.mkdir(exist_ok=True)
    ignore = d / ".gitignore"
    if not ignore.exists():
        # Self-ignoring: never shows up in the user's git status or our snapshots.
        ignore.write_text("*\n", encoding="utf-8")
    return d


def append_history(repo: Path, entry: dict) -> None:
    path = state_dir(repo) / HISTORY_FILE
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def recent_history(repo: Path, limit: int) -> list[dict]:
    path = repo / STATE_DIR / HISTORY_FILE
    if limit <= 0 or not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("verified"):
            rows.append(row)
    return rows[-limit:]


def summarize_for_history(text: str) -> str:
    """First meaningful line(s) of the agent summary, bounded."""
    lines = [ln.strip(" -*#") for ln in text.strip().splitlines() if ln.strip(" -*#")]
    out = " ".join(lines)[:MAX_SUMMARY_CHARS]
    return out + ("..." if len(" ".join(lines)) > MAX_SUMMARY_CHARS else "")


def render_history(rows: list[dict]) -> str:
    parts = []
    for r in rows:
        files = ", ".join(r.get("files", [])[:6])
        parts.append(f"- {r.get('task', '')[:160]} -> files: {files or 'n/a'}. {r.get('summary', '')}")
    return "\n".join(parts)
