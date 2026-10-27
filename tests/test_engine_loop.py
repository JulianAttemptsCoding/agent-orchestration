"""End-to-end orchestration with fake CLIs: gate, checks, resume-repair, isolated review."""
import json

from conftest import FIXED_CALC, git, make_repo
from contextweft.engine import RunSettings, execute, plan_task, run_batch, total_tokens
from contextweft.providers import AgentRun

CHEAT_SKIP = ("import pytest, sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
              "from calc import add\n\n\n@pytest.mark.skip(reason='flaky')\ndef test_add():\n    assert add(2, 3) == 5\n")


def run(repo, provider="codex", settings=None, **kw):
    plan = plan_task("fix the add function in calc", repo, provider=provider, offline=True, **kw)
    return execute(plan, settings=settings or RunSettings(timeout=60))


def test_verified_success_single_call(buggy_repo, agents):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}])
    out = run(buggy_repo)
    assert out["status"] == "verified" and out["success"] and out["verified"]
    assert len(out["runs"]) == 1 and out["repair_rounds"] == 0
    assert out["changed_files"] == ["src/calc.py"]
    assert out["gate"]["findings"] == []
    call = agents.calls()[0]
    assert call["argv"][:4] == ["exec", "--json", "--sandbox", "workspace-write"]
    assert "Engineering rules" in call["prompt"] and "fix the add function" in call["prompt"]
    assert out["checks"][0]["passed"] and out["checks"][0]["name"] == "test"


def test_subq_key_not_passed_to_agent(buggy_repo, agents, monkeypatch):
    monkeypatch.setenv("SUBQ_API_KEY", "sk-test-should-not-leak-1234567890")
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}])
    run(buggy_repo)
    assert agents.calls()[0]["subq_key_visible"] is False


def test_test_skipping_is_blocked_then_repaired_via_resume(buggy_repo, agents):
    agents.scenario([
        {"write": {"tests/test_calc.py": CHEAT_SKIP}, "session": "S1",
         "usage": {"input": 1000, "output": 50, "cached": 0}},
        # Claude reports cumulative totals after --resume.
        {"write": {"tests/test_calc.py": (buggy_repo / "tests/test_calc.py").read_text(), "src/calc.py": FIXED_CALC},
         "session": "S1", "usage": {"input": 1500, "output": 80, "cached": 900}, "cost": 0.03},
    ])
    out = run(buggy_repo, provider="claude")
    assert out["status"] == "verified", out
    assert out["repair_rounds"] == 1
    first, second = agents.calls()
    assert "--resume" not in first["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == "S1"
    assert "test-skipped" in second["prompt"] and "Task:" not in second["prompt"]  # resumed: no task repeat
    # Delta accounting: second run counted as (1500+900)-(1000) input, 80-50 output.
    assert [r["input_tokens"] for r in out["runs"]] == [1000, 1400]
    assert out["totals"]["output_tokens"] == 80
    assert out["totals"]["agent_cost_usd"] == 0.03
    assert "--allowedTools" in first["argv"]
    assert "pytest" in first["argv"][first["argv"].index("--allowedTools") + 1]
    assert "--strict-mcp-config" in first["argv"]


def test_failing_tests_feed_output_into_repair(buggy_repo, agents):
    agents.scenario([{"text": "Done, all tests pass!"}, {"write": {"src/calc.py": FIXED_CALC}}])
    out = run(buggy_repo)
    assert out["status"] == "verified" and out["repair_rounds"] == 1
    repair = agents.calls()[1]
    assert "Failing checks" in repair["prompt"] and "assert" in repair["prompt"]
    assert repair["argv"][:3] == ["exec", "resume", "thread-1"]
    assert "sandbox_mode=workspace-write" in repair["argv"]


def test_claimed_success_without_change_fails_after_rounds(buggy_repo, agents):
    agents.scenario([{"text": "Fixed!"}])
    out = run(buggy_repo, settings=RunSettings(timeout=60, repair_rounds=1))
    assert out["status"] == "failed-verification" and not out["success"]
    assert len(out["runs"]) == 2


def test_budget_threshold_stops_repair(buggy_repo, agents):
    agents.scenario([{"text": "x"}])
    out = run(buggy_repo, settings=RunSettings(timeout=60, max_observed_tokens=50))
    assert len(out["runs"]) == 1
    assert any("threshold" in n for n in out["notes"])


def test_agent_failure_no_repair(buggy_repo, agents):
    agents.scenario([{"exit": 1, "fail": True}])
    out = run(buggy_repo)
    assert out["status"] == "agent-failed" and len(out["runs"]) == 1
    assert "boom" in out["runs"][0]["error"]


def test_timeout_kills_agent(buggy_repo, agents):
    agents.scenario([{"sleep": 30}])
    out = run(buggy_repo, settings=RunSettings(timeout=2))
    assert out["status"] == "agent-failed"
    assert "timeout" in out["runs"][0]["error"]
    assert out["runs"][0]["elapsed_seconds"] < 20


def test_reviewer_runs_in_disposable_copy(buggy_repo, agents):
    agents.scenario([
        {"write": {"src/calc.py": FIXED_CALC}},
        {"write": {"src/calc.py": "VANDALIZED\n", "evil.txt": "x"}, "text": "- looks fine\nVERDICT: PASS"},
    ])
    out = run(buggy_repo, review_when="always", reviewer="claude")
    assert out["review"]["verdict"] == "PASS" and out["status"] == "verified"
    review_call = agents.calls()[1]
    assert review_call["cwd"] != str(buggy_repo)
    assert "--tools" in review_call["argv"] and "plan" in review_call["argv"]
    assert "```diff" in review_call["prompt"] and "+    return a + b" in review_call["prompt"]
    assert (buggy_repo / "src/calc.py").read_text() == FIXED_CALC
    assert not (buggy_repo / "evil.txt").exists()


def test_reviewer_fail_triggers_one_repair(buggy_repo, agents):
    agents.scenario([
        {"write": {"src/calc.py": FIXED_CALC}},
        {"text": "- src/calc.py:2: no overflow handling -> add check\nVERDICT: FAIL"},
        {"write": {"src/calc.py": FIXED_CALC + "\n"}},
    ])
    out = run(buggy_repo, review_when="always", reviewer="claude")
    calls = agents.calls()
    assert len(calls) == 3 and "Independent reviewer findings" in calls[2]["prompt"]
    assert out["review"]["verdict"] == "FAIL" and out["review"]["repaired_after_review"]
    assert out["success"] and out["status"] == "verified"  # repaired result re-passed gate + checks


def test_reviewer_fail_without_rounds_left_fails_run(buggy_repo, agents):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}, {"text": "- bug\nVERDICT: FAIL"}])
    plan = plan_task("fix add", buggy_repo, provider="codex", reviewer="claude", review_when="always", offline=True)
    out = execute(plan, settings=RunSettings(timeout=60, repair_rounds=0))
    assert not out["success"] and out["review"]["verdict"] == "FAIL" and len(agents.calls()) == 2


def test_custom_reviewer_allowed_because_isolated(buggy_repo, agents):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}, {"text": "VERDICT: PASS"}])
    plan = plan_task("fix add", buggy_repo, provider="codex", reviewer="custom", review_when="always", offline=True)
    out = execute(plan, settings=RunSettings(timeout=60, custom_command=agents.custom))
    assert out["review"]["verdict"] == "PASS"


def test_risk_gating(buggy_repo):
    p = plan_task("fix auth bug", buggy_repo, provider="codex", reviewer="claude", review_when="risk", offline=True)
    assert p.reviewer == "claude" and "auth" in p.review_reason
    q = plan_task("format source", buggy_repo, provider="codex", reviewer="claude", review_when="risk", offline=True)
    assert q.review_reason == ""


def test_non_git_repo_skips_gate(tmp_path, agents):
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    agents.scenario([{"write": {"a.py": "x = 2\n"}}])
    plan = plan_task("change x", repo, provider="custom", offline=True)
    out = execute(plan, settings=RunSettings(timeout=60, custom_command=agents.custom))
    assert out["gate"] is None and any("not a git repository" in n for n in out["notes"])
    assert out["status"] == "passed-unverified"


def test_dry_run_and_baseline(buggy_repo, monkeypatch):
    p = plan_task("fix add", buggy_repo, provider="codex", offline=True)
    r = execute(p, dry_run=True)
    assert r["dry_run"] and r["runs"] == [] and r["totals"]["input_tokens"] == 0
    monkeypatch.setattr("contextweft.engine.scout", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    b = plan_task("fix add", buggy_repo, provider="codex", mode="baseline")
    assert b.prompt == "fix add" and b.suggested_files == []


def test_history_flows_into_next_prompt(buggy_repo, agents):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}, "text": "Fixed add() sign error in calc.py"}])
    run(buggy_repo)
    nxt = plan_task("add subtract", buggy_repo, provider="codex", offline=True)
    assert "Recent verified changes" in nxt.prompt and "sign error" in nxt.prompt
    assert git(buggy_repo, "status", "--porcelain").strip() == "M src/calc.py"  # .contextweft self-ignored


def test_batch_stops_on_failure(buggy_repo, agents):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}, {"exit": 1, "fail": True}])
    res = run_batch(["fix add", "break things", "never runs"], buggy_repo,
                    plan_kwargs={"provider": "codex", "offline": True}, settings=RunSettings(timeout=60))
    assert res["attempted"] == 2 and res["succeeded"] == 1
    assert res["results"][0]["status"] == "verified"


def test_unknown_usage_propagates():
    r = total_tokens([AgentRun("custom", "", None, 10, 1, True)], 70)
    assert r["input_tokens"] is None and r["total_tokens"] is None and not r["complete_usage"]
    assert total_tokens([AgentRun("codex", "", 5, 1, 1, True)], None)["input_tokens"] is None


def test_gate_off_mode(buggy_repo, agents):
    agents.scenario([{"write": {"tests/test_calc.py": CHEAT_SKIP}}])
    out = run(buggy_repo, settings=RunSettings(timeout=60, gate="off"))
    # Skipped test "passes" pytest; with the gate off nothing catches it: documents why gate defaults on.
    assert out["gate"] is None and out["success"]


def test_new_repo_without_commits(tmp_path, agents):
    repo = make_repo(tmp_path / "fresh", {"a.py": "x = 1\n"}, commit=False)
    agents.scenario([{"write": {"a.py": "x = 2\n"}}])
    plan = plan_task("change x", repo, provider="codex", offline=True)
    out = execute(plan, settings=RunSettings(timeout=60))
    assert out["changed_files"] == ["a.py"], json.dumps(out)[:500]
