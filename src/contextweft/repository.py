"""Repo discovery and local lexical retrieval. No shell interpolation."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".next",
    ".ssh", ".aws", ".kube", "secrets", "private", "credentials",
    "dist", "build", "coverage", ".subq-scout", ".contextweft", ".pytest_cache", ".mypy_cache",
    ".idea", ".tox", ".cache", "vendor", "target",
}
SKIP_NAMES = {"id_rsa", "id_ed25519", ".npmrc", ".pypirc", "credentials.json", "secrets.json"}
SKIP_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".sqlite", ".db", ".zip",
    ".gz", ".7z", ".tar", ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".pdf", ".docx", ".mp3", ".mp4", ".woff", ".woff2", ".ttf", ".lock",
}
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOP_WORDS = {"the", "and", "is", "a", "in", "to", "on", "for", "from", "of", "with", "add", "fix", "make", "how", "can", "be", "when", "this", "that", "error", "bug", "code", "please", "should"}
MAX_LOCAL_READ_BYTES = 100_000


@dataclass(frozen=True)
class Candidate:
    path: str
    score: float
    excerpt: str = ""


def safe_path(relative: str) -> bool:
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if any(part in SKIP_DIRS or part.endswith((".egg-info", ".dist-info")) for part in p.parts[:-1]):
        return False
    name = p.name.lower()
    if name in SKIP_NAMES or name == ".env" or name.startswith(".env.") or "secret" in name or "credential" in name or name.endswith(".min.js"):
        return False
    return p.suffix.lower() not in SKIP_SUFFIXES


def list_files(root: Path) -> list[str]:
    root = root.resolve()
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True, timeout=10,
        )
        entries = [os.fsdecode(p) for p in r.stdout.split(b"\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        entries = []
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink()]
            entries += [str((Path(base) / f).relative_to(root)) for f in files]
    results = []
    for rel in sorted(set(entries)):
        if not safe_path(rel):
            continue
        full = root / rel
        # Resolve to protect against symlinks and surprising git index entries.
        try:
            if not full.is_file() or full.is_symlink() or not full.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        results.append(rel)
    return results


def terms(text: str) -> set[str]:
    return {s for s in TOKEN_RE.findall(text.lower()) if len(s) > 2 and s not in STOP_WORDS}


def read_sample(root: Path, rel: str, max_bytes: int = MAX_LOCAL_READ_BYTES) -> str:
    try:
        p = root / rel
        if p.stat().st_size > max_bytes or p.is_symlink():
            return ""
        raw = p.read_bytes()
        if b"\0" in raw[:4096]:
            return ""
        return raw.decode("utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def local_candidates(root: Path, task: str, max_candidates: int = 40, snippet_chars: int = 1800) -> list[Candidate]:
    ts = terms(task)
    files = list_files(root)
    scored = []
    for rel in files:
        path_terms = terms(rel.replace("_", " ").replace("-", " "))
        path_hits = ts & path_terms
        content = read_sample(root, rel)
        # Lexical retrieval is a cheap *prefilter*, not a guarantee of relevance.
        content_lower = content.lower()
        body_hits = sum(1 for t in ts if re.search(r"\b" + re.escape(t) + r"\b", content_lower))
        score: float = 6 * len(path_hits) + min(body_hits, 10) + (10 if rel.lower() in task.lower() else 0)
        if rel.startswith(("src/", "app/", "lib/", "tests/")):
            score += 0.5
        if Path(rel).suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cpp", ".c", ".h", ".cs"}:
            score += 1.0
        if score <= 0 and ts:
            continue
        snippet = relevant_excerpt(content, ts, snippet_chars)
        scored.append(Candidate(rel, float(score), snippet))
    # Sparse prompts: fall back to filename order rather than silently returning nothing.
    if not scored:
        scored = [Candidate(rel, 0.0, "") for rel in files[:max_candidates]]
    return sorted(scored, key=lambda c: (-c.score, c.path))[:max_candidates]


def relevant_excerpt(content: str, ts: set[str], limit: int) -> str:
    if not content or limit <= 0:
        return ""
    lines = content.splitlines()
    if not lines:
        return ""
    # Keep the top match region plus beginning of file, bounded before any API call.
    scores = [len(terms(line) & ts) for line in lines]
    best = max(range(len(lines)), key=lambda i: scores[i])
    head = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines[:8]))
    start = max(0, best - 7)
    section = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, min(len(lines), best + 14)))
    return (head + "\n...\n" + section)[:limit]
