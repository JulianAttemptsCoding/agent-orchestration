"""Working-tree snapshots as git tree objects.

Uses a throwaway index file, so the user's index, HEAD, branches and stash are
never touched. Only loose objects are written (normal git gc cleans them up).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

GIT_TIMEOUT = 120


class GitError(RuntimeError):
    pass


def _git_bytes(repo: Path, *args: str, env: dict[str, str] | None = None,
               check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env,
                            timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed ({type(exc).__name__})") from exc
    if check and cp.returncode != 0:
        raise GitError(f"git {args[0]} failed: {cp.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return cp


def _git(repo: Path, *args: str, env: dict[str, str] | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = _git_bytes(repo, *args, env=env, check=check)
    return subprocess.CompletedProcess(cp.args, cp.returncode, cp.stdout.decode("utf-8", "replace"),
                                       cp.stderr.decode("utf-8", "replace"))


def is_git_repo(repo: Path) -> bool:
    try:
        cp = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    except GitError:
        return False
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def snapshot(repo: Path) -> str:
    """Tree id of the whole working tree (tracked + untracked, honoring .gitignore)."""
    index_path = _git(repo, "rev-parse", "--git-path", "index").stdout.strip()
    real_index = Path(index_path) if os.path.isabs(index_path) else repo / index_path
    fd, tmp = tempfile.mkstemp(prefix="contextweft-index-")
    os.close(fd)
    try:
        if real_index.is_file():
            # Warm stat cache (fast on big repos). copy2 keeps the index mtime, which git's
            # racy-clean check needs to re-hash files edited in the same second.
            shutil.copy2(real_index, tmp)
        else:
            os.unlink(tmp)
        env = {**os.environ, "GIT_INDEX_FILE": tmp}
        _git(repo, "add", "-A", "--", ".", env=env)
        return _git(repo, "write-tree", env=env).stdout.strip()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def tree_of(repo: Path, ref: str) -> str:
    """Tree id for ``ref``; the empty tree when the repo has no commits yet."""
    cp = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}", check=False)
    if cp.returncode == 0:
        return cp.stdout.strip()
    if ref != "HEAD":
        raise GitError(f"unknown ref: {ref}")
    try:
        empty = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tree", "-w", "--stdin"],
                               input="", capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError("could not create empty tree") from exc
    return empty.stdout.strip()


def diff(repo: Path, before: str, after: str, *, context: int = 0) -> str:
    return _git(repo, "diff", "--no-color", "--no-ext-diff", f"--unified={context}", "-M",
                before, after).stdout


def changed_files(repo: Path, before: str, after: str) -> list[tuple[str, str]]:
    """[(status letter, path)] where status is A/M/D/R (rename reports new path)."""
    out = _git(repo, "diff", "--name-status", "-z", "-M", before, after).stdout
    parts = [p for p in out.split("\0") if p]
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i][0]
        if status in "RC":
            result.append((status, parts[i + 2]))
            i += 3
        else:
            result.append((status, parts[i + 1]))
            i += 2
    return result


def show_file(repo: Path, tree: str, path: str) -> str | None:
    cp = _git(repo, "show", f"{tree}:{path}", check=False)
    return cp.stdout if cp.returncode == 0 else None


def export_tree(repo: Path, tree: str, dest: Path) -> None:
    """Materialize a snapshot in ``dest`` (no .git) for isolated, disposable review."""
    data = _git_bytes(repo, "archive", "--format=tar", tree).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # pragma: no cover - Python without PEP 706 backport
            for member in tar.getmembers():
                target = (dest / member.name).resolve()
                if not target.is_relative_to(dest.resolve()) or member.issym() or member.islnk():
                    raise GitError(f"Unsafe archive member: {member.name}")
            tar.extractall(dest)  # noqa: S202 - members validated above


def clone_at(repo: Path, ref: str, dest: Path) -> None:
    """Independent local clone checked out at ``ref`` (benchmark isolation)."""
    try:
        cp = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(dest)],
                            capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git clone failed ({type(exc).__name__})") from exc
    if cp.returncode != 0:
        raise GitError(f"git clone failed: {cp.stderr.strip()[:300]}")
    _git(dest, "checkout", "--quiet", "--detach", ref)
