"""Ground-truth checks (tests, lint) run by the orchestrator, not by the agent."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .procs import redact, run_process, split_command, tail

OUTPUT_TAIL_CHARS = 1800


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: str
    passed: bool
    returncode: int | None
    elapsed_seconds: float
    output_tail: str
    error: str = ""

    def to_json(self, *, include_output: bool = True) -> dict:
        d = asdict(self)
        if not include_output:
            d.pop("output_tail")
        return d


def run_check(name: str, command: str, repo: Path, timeout: int) -> CheckResult:
    start = time.monotonic()
    try:
        args = split_command(command)
        res = run_process(args, cwd=repo, timeout=timeout)
    except FileNotFoundError:
        return CheckResult(name, command, False, None, 0.0, "", f"command not found: {command.split()[0]}")
    except ValueError as exc:
        return CheckResult(name, command, False, None, 0.0, "", str(exc))
    elapsed = round(time.monotonic() - start, 3)
    # Failure output is the most useful context for a repair round; keep only the tail.
    output = redact(tail(res.stdout + ("\n" + res.stderr if res.stderr.strip() else ""), OUTPUT_TAIL_CHARS))
    if res.timed_out:
        return CheckResult(name, command, False, None, elapsed, output, f"timed out after {timeout}s")
    return CheckResult(name, command, res.returncode == 0, res.returncode, elapsed, output)


def run_checks(commands: dict[str, str | None], repo: Path, timeout: int) -> list[CheckResult]:
    return [run_check(name, cmd, repo, timeout) for name, cmd in commands.items() if cmd]


def render_failures(results: list[CheckResult]) -> str:
    parts = []
    for r in results:
        if not r.passed:
            why = r.error or f"exit {r.returncode}"
            parts.append(f"`{r.command}` failed ({why}):\n{r.output_tail}")
    return "\n\n".join(parts)
