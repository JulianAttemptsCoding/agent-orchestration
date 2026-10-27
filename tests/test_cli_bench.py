"""CLI commands and the A/B benchmark harness (fake agents only)."""
import io
import json

import pytest

from conftest import BUGGY, FIXED_CALC, make_repo
from contextweft.cli import install_skills, main, read_tasks


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_scout_cli(buggy_repo, capsys):
    assert main(["scout", "fix calc add", "--repo", str(buggy_repo), "--offline"]) == 0
    assert "src/calc.py" in [f["path"] for f in out_json(capsys)["files"]]


def test_doctor_reports_problems(tmp_path, capsys):
    assert main(["doctor", "--repo", str(tmp_path)]) == 0
    d = out_json(capsys)
    assert d["repo_is_git"] is False and any("not a git" in p for p in d["problems"])


def test_init_and_no_overwrite(tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert main(["init", "--repo", str(tmp_path)]) == 0
    assert 'test = "npm test --silent"' in (tmp_path / "contextweft.toml").read_text()
    capsys.readouterr()
    assert main(["init", "--repo", str(tmp_path)]) == 2


def test_install_skill_is_non_destructive(tmp_path):
    files = install_skills(tmp_path)
    assert len(files) == 2
    with pytest.raises(ValueError):
        install_skills(tmp_path)


def test_gate_cli_and_claude_hook(buggy_repo, capsys, monkeypatch):
    assert main(["gate", "--repo", str(buggy_repo)]) == 0
    assert out_json(capsys)["blocked"] is False
    (buggy_repo / "src/calc.py").write_text("def add(a, b):\n    breakpoint()\n    return a + b\n")
    assert main(["gate", "--repo", str(buggy_repo)]) == 1
    assert out_json(capsys)["findings"][0]["rule"] == "debug-leftover"
    payload = {"cwd": str(buggy_repo), "stop_hook_active": False}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["gate", "--hook", "claude"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "debug-leftover" in captured.err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "stop_hook_active": True})))
    assert main(["gate", "--hook", "claude"]) == 0  # never loops


def test_plan_cli(buggy_repo, capsys):
    assert main(["plan", "fix calc add", "--repo", str(buggy_repo), "--offline", "--provider", "codex"]) == 0
    plan = out_json(capsys)
    assert "src/calc.py" in plan["suggested_files"] and "Engineering rules" in plan["prompt"]
    assert main(["plan", "  ", "--repo", str(buggy_repo), "--offline", "--provider", "codex"]) == 2


def test_gate_uses_committed_config_not_agent_edits(buggy_repo, capsys):
    (buggy_repo / "contextweft.toml").write_text('[quality]\ndisable = ["debug-leftover", "config-tamper"]\n')
    (buggy_repo / "src/calc.py").write_text("def add(a, b):\n    breakpoint()\n    return a + b\n")
    assert main(["gate", "--repo", str(buggy_repo)]) == 1
    res = out_json(capsys)
    assert res["config_source"] == "HEAD:contextweft.toml"
    assert {"debug-leftover", "config-tamper"} <= {f["rule"] for f in res["findings"]}


def test_gate_uncommitted_new_config_is_used_outside_hooks(tmp_path, capsys):
    repo = make_repo(tmp_path / "n", {"a.py": "x = 1\n"})
    assert main(["init", "--repo", str(repo)]) == 0
    capsys.readouterr()
    assert main(["gate", "--repo", str(repo)]) == 0  # a fresh init must not block
    res = out_json(capsys)
    assert res["config_source"] == "working tree" and not res["blocked"]


def test_run_dry_run_and_record_requires_case(buggy_repo, capsys):
    assert main(["run", "fix add", "--repo", str(buggy_repo), "--provider", "codex", "--offline", "--dry-run"]) == 0
    assert out_json(capsys)["dry_run"] is True
    assert main(["run", "fix add", "--repo", str(buggy_repo), "--provider", "codex", "--record", "x.jsonl"]) == 2


def test_run_cli_records_sanitized(buggy_repo, agents, tmp_path, capsys):
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}, "text": "SECRET MODEL PROSE"}])
    rec = tmp_path / "rec.jsonl"
    code = main(["run", "fix add", "--repo", str(buggy_repo), "--provider", "codex", "--offline",
                 "--case-id", "c1", "--record", str(rec)])
    assert code == 0 and out_json(capsys)["status"] == "verified"
    row = json.loads(rec.read_text())
    assert row["case_id"] == "c1" and "SECRET MODEL PROSE" not in rec.read_text()
    assert "output_tail" not in json.dumps(row["checks"])


def test_read_tasks(tmp_path):
    f = tmp_path / "t.md"
    f.write_text("# plan\n- [x] done already\n- [ ] first task\n* second task\n\nthird\n")
    assert read_tasks(f) == ["first task", "second task", "third"]


def test_bench_end_to_end(tmp_path, agents, capsys):
    repo = make_repo(tmp_path / "bench", BUGGY)
    cases = tmp_path / "cases.jsonl"
    test_cmd = BUGGY["contextweft.toml"].split('"')[1]
    cases.write_text("".join(json.dumps({"id": f"k{i}", "task": "fix add in calc", "test": test_cmd}) + "\n"
                             for i in range(2)))
    agents.scenario([{"write": {"src/calc.py": FIXED_CALC}}])
    out = tmp_path / "results"
    code = main(["bench", str(cases), "--repo", str(repo), "--out", str(out), "--provider", "codex"])
    report = out_json(capsys)
    assert code == 0 and report["paired_cases"] == 2
    assert report["metrics"]["verified_success"]["baseline_rate"] == 1.0
    base_rows = [json.loads(x) for x in (out / "baseline.jsonl").read_text().splitlines()]
    assert all(r["arm"] == "baseline" and r["verified_success"] for r in base_rows)
    prompts = [c["prompt"] for c in agents.calls()]
    assert prompts.count("fix add in calc") == 2  # baseline arm gets the bare task
    assert sum("Engineering rules" in p for p in prompts) == 2
    assert all(c["cwd"] != str(repo) for c in agents.calls())  # fresh clones only
    assert (repo / "src/calc.py").read_text() == BUGGY["src/calc.py"]
    assert main(["bench", str(cases), "--repo", str(repo), "--out", str(out), "--provider", "codex"]) == 2
