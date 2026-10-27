"""Subprocess helpers: no shell, whole-tree kill on timeout, scrubbed env, redaction."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Credentials the orchestrator itself uses; coding agents never need them.
SCRUBBED_ENV = ("SUBQ_API_KEY",)

# Each pattern's group 1 is a prefix that stays visible; the rest is masked.
SECRET_PATTERNS = [
    re.compile(r"\b()sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b()gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\b()github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b()AKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b()xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"()-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*[\"']?)[^\s\"']{6,}"),
]


def redact(text: str) -> str:
    """Mask common credential shapes before text reaches logs or prompts."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


def tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


def split_command(command: str) -> list[str]:
    """Parse a configured command without a shell. Windows paths keep backslashes."""
    args = shlex.split(command, posix=not IS_WINDOWS)
    if IS_WINDOWS:
        args = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a for a in args]
    if not args:
        raise ValueError("Empty command")
    return args


def resolve_executable(name: str) -> str | None:
    """Full path, so Windows .cmd/.exe shims resolve without shell=True."""
    if os.path.sep in name or (os.altsep and os.altsep in name):
        return name if Path(name).is_file() else None
    return shutil.which(name)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        # taskkill /T reaches node/python grandchildren behind .cmd shims.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run_process(args: list[str], *, cwd: Path, timeout: float, stdin: str | None = None,
                env: dict[str, str] | None = None) -> ProcResult:
    """Run without a shell. Raises FileNotFoundError if the executable is missing."""
    exe = resolve_executable(args[0])
    if exe is None:
        raise FileNotFoundError(args[0])
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [exe, *args[1:]], cwd=cwd, env=env if env is not None else child_env(),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return ProcResult(proc.returncode, out or "", err or "", False)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        return ProcResult(proc.returncode if proc.returncode is not None else -9,
                          out or "", err or "", True)


def remove_tree(path: Path) -> None:
    """rmtree that also clears read-only files (git objects on Windows)."""
    def retry_writable(func: Callable[[str], object], p: str, _exc: object) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)
