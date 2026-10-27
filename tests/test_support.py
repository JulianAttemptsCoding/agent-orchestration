"""Config, procs, metrics, prompts."""
import json
import sys

import pytest

from contextweft.config import Config, load_config, render_template
from contextweft.metrics import compare_logs, mcnemar_exact
from contextweft.procs import redact, run_process, split_command
from contextweft.prompts import parse_verdict, primary_prompt, review_prompt


def test_template_parses_and_detects_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "contextweft.toml").write_text(render_template(tmp_path), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.test_command == "python -m pytest -q" and cfg.gate == "block" and cfg.lean


def test_config_rejects_unknown_and_bad_types(tmp_path):
    p = tmp_path / "contextweft.toml"
    p.write_text("[quality]\nbogus = 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        load_config(tmp_path)
    p.write_text("[quality]\nrepair_rounds = 'two'\n")
    with pytest.raises(ValueError, match="invalid type"):
        load_config(tmp_path)
    p.write_text("[quality]\ngate = 'maybe'\n")
    with pytest.raises(ValueError, match="gate"):
        load_config(tmp_path)
    p.write_text("[agent]\nmax_budget_usd = 2\n")
    assert load_config(tmp_path).max_budget_usd == 2.0
    assert Config().to_json()["repair_rounds"] == 2


def test_redact():
    s = redact("key sk-ant-abcdefghijklmnopqrstuvwx and ghp_abcdefghijklmnopqrstuvwxyz12 "
               "Bearer abcdefghijklmnop password=hunter2xyz")
    assert "abcdefghijklmnopqrstuvwx" not in s and "ghp_abc" not in s and "hunter2" not in s
    assert s.count("[REDACTED]") == 4 and "Bearer [REDACTED]" in s


def test_split_command_keeps_windows_paths():
    args = split_command(f'"{sys.executable}" -m pytest -q')
    assert args[0] == sys.executable and args[1:] == ["-m", "pytest", "-q"]
    with pytest.raises(ValueError):
        split_command("   ")


def test_run_process_no_shell(tmp_path):
    # Shell metacharacters stay literal: there is no shell to interpret them.
    res = run_process([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo pwned && $(x)"],
                      cwd=tmp_path, timeout=30)
    assert res.stdout.strip() == "a; echo pwned && $(x)" and res.returncode == 0


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_compare_logs_paired_stats(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write(a, [{"case_id": f"c{i}", "totals": {"total_tokens": 1000 + i}, "elapsed_seconds": 10,
                "verified_success": i % 2 == 0} for i in range(12)])
    _write(b, [{"case_id": f"c{i}", "totals": {"total_tokens": 700 + i}, "elapsed_seconds": 9,
                "verified_success": True} for i in range(12)])
    res = compare_logs(a, b)
    tok = res["metrics"]["total_tokens"]
    assert tok["median_paired_diff"] == -300 and tok["significant"] is True
    assert res["metrics"]["verified_success"]["only_assisted_passed"] == 6
    assert res["metrics"]["input_tokens"]["status"] == "unknown_or_missing"


def test_compare_small_sample_no_inference(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write(a, [{"case_id": "x", "totals": {"input_tokens": 100, "output_tokens": 50}, "elapsed_seconds": 30}])
    _write(b, [{"case_id": "x", "totals": {"input_tokens": 80, "output_tokens": 40}, "elapsed_seconds": 25}])
    res = compare_logs(a, b)
    assert res["metrics"]["total_tokens"]["change_percent"] == -20
    assert res["metrics"]["total_tokens"]["significant"] is None and "inference" in res
    _write(b, [{"case_id": "y"}])
    with pytest.raises(ValueError):
        compare_logs(a, b)


def test_mcnemar():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)


def test_prompts():
    p = primary_prompt("Do X", files=["a.py"], checks=["pytest"], conventions="Use tabs", history="- did Y")
    assert "a.py" in p and "`pytest`" in p and "Use tabs" in p and "did Y" in p
    r = review_prompt("Do X", diff="+x" * 20000, gate="", checks="")
    assert "truncated" in r and "VERDICT" in r
    assert parse_verdict("blah\n**VERDICT: fail**") == "FAIL"
    assert parse_verdict("no verdict") is None
