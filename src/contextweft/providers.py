"""Isolated subprocess adapters. No shell; prompts go over stdin; usage never invented."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .procs import redact, resolve_executable, run_process, split_command, tail

REVIEW_TOOLS = "Read,Grep,Glob"


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentOptions:
    review: bool = False
    max_turns: int = 40
    timeout: int = 1800
    model: str | None = None
    lean: bool = True
    allowed_tools: tuple[str, ...] = ()
    max_budget_usd: float | None = None
    resume: str | None = None
    custom_command: str | None = None


@dataclass(frozen=True)
class AgentRun:
    provider: str
    text: str
    input_tokens: int | None
    output_tokens: int | None
    elapsed_seconds: float
    success: bool
    error: str = ""
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    # Claude reports whole-conversation totals after --resume; engine converts to deltas.
    usage_cumulative: bool = False
    error_detail: str = ""
    role: str = "primary"

    def to_json(self) -> dict:
        return asdict(self)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def parse_claude_output(text: str, *, elapsed: float, returncode: int) -> AgentRun:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return AgentRun("claude", "", None, None, elapsed, False, "Claude did not return JSON")
    if not isinstance(data, dict):
        return AgentRun("claude", "", None, None, elapsed, False, "Unexpected Claude response")
    # result.usage can describe only the final request; modelUsage aggregates the
    # whole run (including subagents) and takes precedence.
    model_usage = data.get("modelUsage")
    total_in = total_out = cached = None
    if isinstance(model_usage, dict) and model_usage and all(isinstance(v, dict) for v in model_usage.values()):
        groups = list(model_usage.values())
        if all(_count(g.get("inputTokens")) is not None and _count(g.get("outputTokens")) is not None
               for g in groups):
            total_in = sum(g["inputTokens"] + (_count(g.get("cacheReadInputTokens")) or 0)
                           + (_count(g.get("cacheCreationInputTokens")) or 0) for g in groups)
            total_out = sum(g["outputTokens"] for g in groups)
            cached = sum(_count(g.get("cacheReadInputTokens")) or 0 for g in groups)
    else:
        usage = _dict(data.get("usage"))
        inp = _count(usage.get("input_tokens"))
        if inp is not None:
            total_in = inp + (_count(usage.get("cache_read_input_tokens")) or 0) + (
                _count(usage.get("cache_creation_input_tokens")) or 0)
            cached = _count(usage.get("cache_read_input_tokens"))
        total_out = _count(usage.get("output_tokens"))
    result = data.get("result")
    price = data.get("total_cost_usd")
    session = data.get("session_id")
    ok = returncode == 0 and isinstance(result, str) and not data.get("is_error")
    error = ""
    if not ok:
        subtype = data.get("subtype")
        error = {"error_max_turns": "Claude hit --max-turns",
                 "error_max_budget_usd": "Claude hit --max-budget-usd"}.get(
            subtype if isinstance(subtype, str) else "", "")
        if not error:
            # API/auth failures arrive as is_error with the reason in `result` (subtype may say "success").
            reason = redact(result.strip())[:200] if data.get("is_error") and isinstance(result, str) else ""
            error = f"Claude execution failed: {reason}" if reason else "Claude execution failed"
    return AgentRun(
        "claude", result if isinstance(result, str) else "", total_in, total_out, elapsed, ok, error,
        cached, float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price >= 0 else None,
        session if isinstance(session, str) and session else None,
    )


def parse_codex_output(text: str, *, elapsed: float, returncode: int) -> AgentRun:
    in_count = out_count = cache = 0
    got_usage = False
    latest_text = ""
    failure_msg = ""
    completed = False
    thread_id = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "thread.started" and isinstance(row.get("thread_id"), str):
            thread_id = row["thread_id"]
        elif kind == "turn.failed":
            err = row.get("error")
            msg = err.get("message") if isinstance(err, dict) else None
            failure_msg = msg if isinstance(msg, str) and msg else "turn failed"
        elif kind == "turn.completed":
            completed = True
            u = row.get("usage")
            if isinstance(u, dict):
                inp, out = _count(u.get("input_tokens")), _count(u.get("output_tokens"))
                if inp is not None and out is not None:
                    # cached_input_tokens is a subset of input_tokens: not additive.
                    in_count += inp
                    out_count += out
                    cache += _count(u.get("cached_input_tokens")) or 0
                    got_usage = True
        elif kind == "item.completed":
            # Standalone `error` events (e.g. reconnect notices) are non-fatal by design.
            item = row.get("item")
            if isinstance(item, dict) and (item.get("type") or item.get("item_type")) in (
                    "agent_message", "assistant_message") and isinstance(item.get("text"), str):
                latest_text = item["text"]
    ok = returncode == 0 and not failure_msg and completed
    error = "" if ok else (f"Codex turn failed: {failure_msg}" if failure_msg
                           else "Codex execution failed or incomplete")
    return AgentRun("codex", latest_text, in_count if got_usage else None,
                    out_count if got_usage else None, elapsed, ok, redact(error)[:300],
                    cache if got_usage else None, None, thread_id)


def parse_custom_output(text: str, *, elapsed: float, returncode: int) -> AgentRun:
    """Custom adapter protocol: one JSON object on stdout with text and optional usage."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return AgentRun("custom", "", None, None, elapsed, False, "Custom adapter must output JSON")
    if not isinstance(d, dict) or not isinstance(d.get("text"), str):
        return AgentRun("custom", "", None, None, elapsed, False, "Custom adapter missing text")
    usage = _dict(d.get("usage"))
    ok = returncode == 0 and not d.get("error")
    return AgentRun("custom", d["text"], _count(usage.get("input_tokens")),
                    _count(usage.get("output_tokens")), elapsed, ok,
                    "" if ok else "Custom adapter failed",
                    _count(usage.get("cached_input_tokens")))


def available(name: str) -> bool:
    return resolve_executable(name) is not None


def resolve_provider(name: str) -> str:
    if name != "auto":
        return name
    for candidate in ("claude", "codex"):
        if available(candidate):
            return candidate
    raise ProviderError("Neither claude nor codex is on PATH")


def build_command(name: str, opts: AgentOptions) -> list[str]:
    if name == "claude":
        args = ["claude", "-p", "--output-format", "json", "--max-turns", str(opts.max_turns),
                "--permission-mode", "plan" if opts.review else "acceptEdits"]
        if opts.review:
            args += ["--tools", REVIEW_TOOLS]
        elif opts.allowed_tools:
            args += ["--allowedTools", ",".join(opts.allowed_tools)]
        if opts.lean:
            # Project/user MCP servers and skill listings cost fixed tokens every turn
            # and run unprompted under -p; lean mode loads neither.
            args += ["--strict-mcp-config", "--disable-slash-commands"]
        if opts.model:
            args += ["--model", opts.model]
        if opts.max_budget_usd is not None:
            args += ["--max-budget-usd", f"{opts.max_budget_usd:g}"]
        if opts.resume:
            args += ["--resume", opts.resume]
        return args
    if name == "codex":
        sandbox = "read-only" if opts.review else "workspace-write"
        if opts.resume:
            # `exec resume` has no --sandbox flag; a non-TOML -c value is taken literally.
            args = ["codex", "exec", "resume", opts.resume, "--json", "-c", f"sandbox_mode={sandbox}"]
        else:
            args = ["codex", "exec", "--json", "--sandbox", sandbox]
        if opts.review:
            args.append("--skip-git-repo-check")  # reviewers run in a disposable export
        # No lean flag for Codex: --ignore-user-config saves ~17% of a minimal request (measured)
        # but also drops settings runs depend on, e.g. `[windows] sandbox = "elevated"`, without
        # which the Windows sandbox blocks every read and edit. `-c mcp_servers={}` saves nothing.
        if opts.model:
            args += ["-m", opts.model]
        return args + ["-"]
    if name == "custom":
        if not opts.custom_command:
            raise ProviderError("Custom provider needs --custom-command (no shell; shlex parsed)")
        try:
            return split_command(opts.custom_command)
        except ValueError as exc:
            raise ProviderError(f"Invalid custom command: {exc}") from exc
    raise ProviderError(f"Unknown provider: {name}")


PARSERS = {"claude": parse_claude_output, "codex": parse_codex_output, "custom": parse_custom_output}


def run_agent(provider: str, prompt: str, *, repo: Path, opts: AgentOptions) -> AgentRun:
    if opts.max_turns < 1 or opts.timeout < 1:
        raise ValueError("max_turns and timeout must be positive")
    name = resolve_provider(provider)
    args = build_command(name, opts)
    start = time.monotonic()
    try:
        res = run_process(args, cwd=repo, timeout=opts.timeout, stdin=prompt)
    except FileNotFoundError as exc:
        raise ProviderError(f"Command not found: {args[0]}") from exc
    except OSError as exc:
        raise ProviderError(f"{name} failed to launch ({type(exc).__name__})") from exc
    elapsed = round(time.monotonic() - start, 3)
    result = PARSERS[name](res.stdout, elapsed=elapsed, returncode=res.returncode)
    detail = redact(tail(res.stderr, 400)) if not result.success else ""
    error = result.error
    if res.timed_out:
        error = f"{name} exceeded {opts.timeout}s timeout and was killed; edits may be partial"
    elif not result.success and not error:
        error = f"CLI exited with status {res.returncode}"
    return AgentRun(name, result.text, result.input_tokens, result.output_tokens, elapsed,
                    result.success and not res.timed_out, error, result.cached_input_tokens,
                    result.cost_usd, result.session_id,
                    usage_cumulative=name == "claude" and opts.resume is not None,
                    error_detail=detail, role="review" if opts.review else "primary")
