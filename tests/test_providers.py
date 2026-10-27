import json
import sys

import pytest

from contextweft.providers import (
    AgentOptions,
    ProviderError,
    build_command,
    parse_claude_output,
    parse_codex_output,
    parse_custom_output,
    run_agent,
)


def test_claude_total_includes_cache_and_all_models():
    result = parse_claude_output(json.dumps({
        "is_error": False, "result": "ok", "total_cost_usd": 0.21,
        "usage": {"input_tokens": 999999, "output_tokens": 999999},
        "modelUsage": {
            "sonnet": {"inputTokens": 100, "cacheReadInputTokens": 200,
                       "cacheCreationInputTokens": 50, "outputTokens": 20},
            "haiku": {"inputTokens": 10, "cacheReadInputTokens": 40,
                      "cacheCreationInputTokens": 0, "outputTokens": 5},
        }
    }), elapsed=1, returncode=0)
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (400, 25, 240)
    assert result.cost_usd == 0.21


def test_claude_missing_usage_is_unknown():
    result = parse_claude_output('{"result":"ok","is_error":false}', elapsed=0.1, returncode=0)
    assert result.input_tokens is None and result.output_tokens is None


def test_claude_usage_fallback_adds_cached_tokens():
    result = parse_claude_output(json.dumps({"result": "ok", "usage": {
        "input_tokens": 20, "cache_read_input_tokens": 150, "cache_creation_input_tokens": 30,
        "output_tokens": 8}}), elapsed=1, returncode=0)
    assert result.input_tokens == 200
    assert result.output_tokens == 8


def test_codex_multiturn_cached_not_double_counted():
    rows = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "turn.completed", "usage": {"input_tokens": 90, "cached_input_tokens": 80, "output_tokens": 10}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
        {"type": "turn.completed", "usage": {"input_tokens": 70, "cached_input_tokens": 45, "output_tokens": 11}},
    ]
    r = parse_codex_output("\n".join(json.dumps(row) for row in rows), elapsed=2, returncode=0)
    assert (r.input_tokens, r.output_tokens, r.cached_input_tokens, r.text) == (160, 21, 125, "final")


def test_codex_unknown_usage_not_zero():
    r = parse_codex_output('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}', elapsed=1, returncode=0)
    assert r.input_tokens is None


def test_codex_failure_event():
    r = parse_codex_output('{"type":"turn.failed"}', elapsed=1, returncode=0)
    assert not r.success


def test_custom_provider_real_process(tmp_path):
    script = tmp_path / "adapter.py"
    script.write_text('import sys,json\ns=sys.stdin.read()\nprint(json.dumps({"text":s,"usage":{"input_tokens":123,"output_tokens":4}}))\n')
    r = run_agent("custom", "hello", repo=tmp_path,
                  opts=AgentOptions(custom_command=f'"{sys.executable}" "{script}"'))
    assert r.success and r.text == "hello" and r.input_tokens == 123


def test_custom_missing_usage_unknown():
    r = parse_custom_output('{"text":"hello"}', elapsed=0.01, returncode=0)
    assert r.input_tokens is None and r.output_tokens is None


def test_codex_thread_id_assistant_message_and_nonfatal_error():
    rows = [
        {"type": "thread.started", "thread_id": "T9"},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"id": "a", "item_type": "assistant_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}},
    ]
    r = parse_codex_output("\n".join(json.dumps(x) for x in rows), elapsed=1, returncode=0)
    assert r.success and r.session_id == "T9" and r.text == "ok"


def test_codex_turn_failed_message_is_redacted():
    r = parse_codex_output(json.dumps({"type": "turn.failed", "error": {"message": "bad key sk-abcdefghijklmnopqrstuv"}}),
                           elapsed=1, returncode=1)
    assert not r.success and "sk-abc" not in r.error and "REDACTED" in r.error


def test_claude_max_turns_and_session():
    r = parse_claude_output(json.dumps({"subtype": "error_max_turns", "is_error": True, "session_id": "s",
                                        "result": ""}), elapsed=1, returncode=1)
    assert not r.success and "max-turns" in r.error and r.session_id == "s"


def test_claude_bool_usage_rejected():
    r = parse_claude_output(json.dumps({"result": "ok", "usage": {"input_tokens": True, "output_tokens": 3}}),
                            elapsed=1, returncode=0)
    assert r.input_tokens is None


def test_build_command_claude_primary_review_resume():
    primary = build_command("claude", AgentOptions(allowed_tools=("Bash(npm test)",), model="sonnet",
                                                   max_budget_usd=1.5))
    assert primary[:3] == ["claude", "-p", "--output-format"]
    assert primary[primary.index("--permission-mode") + 1] == "acceptEdits"
    assert primary[primary.index("--allowedTools") + 1] == "Bash(npm test)"
    assert "--strict-mcp-config" in primary and "--disable-slash-commands" in primary
    assert primary[primary.index("--max-budget-usd") + 1] == "1.5"
    review = build_command("claude", AgentOptions(review=True, lean=False, resume="abc"))
    assert review[review.index("--permission-mode") + 1] == "plan"
    assert review[review.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--allowedTools" not in review and "--strict-mcp-config" not in review
    assert review[-2:] == ["--resume", "abc"]
    assert "--dangerously-skip-permissions" not in primary + review


def test_build_command_codex():
    assert build_command("codex", AgentOptions()) == ["codex", "exec", "--json", "--sandbox", "workspace-write", "-"]
    rv = build_command("codex", AgentOptions(review=True, model="o4"))
    assert rv[rv.index("--sandbox") + 1] == "read-only" and "--skip-git-repo-check" in rv and rv[-1] == "-"
    rs = build_command("codex", AgentOptions(resume="T1"))
    assert rs[:4] == ["codex", "exec", "resume", "T1"] and "sandbox_mode=workspace-write" in rs
    assert not any("danger" in a for a in rv + rs)


def test_custom_requires_command_and_unknown_provider():
    with pytest.raises(ProviderError):
        build_command("custom", AgentOptions())
    with pytest.raises(ProviderError):
        build_command("gpt", AgentOptions())


def test_missing_cli_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ProviderError):
        run_agent("codex", "x", repo=tmp_path, opts=AgentOptions())
