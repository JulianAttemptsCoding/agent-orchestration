"""Each gate rule against a real git diff."""
import pytest

from conftest import make_repo
from contextweft import gitsnap
from contextweft.config import Config
from contextweft.quality import evaluate_diff, parse_diff

BASE = {
    "src/app.py": "def run(x):\n    return x * 2\n",
    "src/util.py": ("def normalize(rows):\n    out = []\n    for row in rows:\n        key = row['id'].strip()\n"
                    "        value = row['value'] or 0\n        out.append((key, value * 2))\n"
                    "    out.sort(key=lambda r: r[0])\n    return out\n"),
    "tests/test_app.py": "from app import run\n\n\ndef test_run():\n    assert run(2) == 4\n    assert run(0) == 0\n",
    "package.json": '{"name": "x", "dependencies": {}}\n',
}


def gate(tmp_path, changes, **overrides):
    repo = make_repo(tmp_path / "r", BASE)
    before = gitsnap.snapshot(repo)
    for rel, content in changes.items():
        p = repo / rel
        if content is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    after = gitsnap.snapshot(repo)
    cfg = Config(**overrides)
    res = evaluate_diff(repo, gitsnap.diff(repo, before, after), test_globs=cfg.test_globs,
                        allowed_paths=cfg.allowed_paths, max_changed_lines=cfg.max_changed_lines,
                        duplicate_min_lines=cfg.duplicate_min_lines, disabled_rules=frozenset(cfg.disabled_rules), exclude_paths=cfg.exclude_paths)
    return res, {(f.rule, f.severity) for f in res.findings}


def test_clean_change_passes(tmp_path):
    res, rules = gate(tmp_path, {"src/app.py": "def run(x):\n    return x * 3\n"})
    assert not res.blocked and rules == set()
    assert (res.files_changed, res.lines_added, res.lines_removed) == (1, 1, 1)


@pytest.mark.parametrize("change,rule,severity", [
    ({"tests/test_app.py": None}, "test-deleted", "block"),
    ({"tests/test_app.py": "from app import run\n\n\ndef test_run():\n    run(2)\n"}, "assertions-removed", "block"),
    ({"tests/test_app.py": BASE["tests/test_app.py"].replace("def test_run", "@pytest.mark.skip\ndef test_run")},
     "test-skipped", "block"),
    ({"tests/test_app.py": BASE["tests/test_app.py"] + "\n\ndef test_more():\n    assert True\n"}, "trivial-assert", "warn"),
    ({"src/app.py": "import os\ndef run(x):\n    if os.environ.get('PYTEST_CURRENT_TEST'):\n        return 4\n    return x * 2\n"},
     "test-special-case", "block"),
    ({"src/app.py": "def run(x):\n    return x * 2  # type: ignore\n"}, "suppression", "warn"),
    ({"src/app.py": "def run(x):\n    try:\n        return x * 2\n    except Exception:\n        pass\n"},
     "swallowed-error", "warn"),
    ({"src/app.py": "def run(x):\n    try:\n        return x * 2\n    except: pass\n"}, "swallowed-error", "warn"),
    ({"src/web.js": "try { go() } catch (e) {}\n"}, "swallowed-error", "warn"),
    ({"src/app.py": "def run(x):\n    # In a real implementation we would validate x\n    return x * 2\n"}, "placeholder", "block"),
    ({"src/app.py": "def run(x):\n    raise NotImplementedError\n"}, "placeholder", "warn"),
    ({"src/app.py": "def run(x):\n    # TODO handle negatives\n    return x * 2\n"}, "todo", "warn"),
    ({"src/app.py": "def run(x):\n    breakpoint()\n    return x * 2\n"}, "debug-leftover", "block"),
    ({"src/web.ts": "console.log('hi')\n"}, "debug-leftover", "warn"),
    ({"package.json": '{"name": "x", "dependencies": {"left-padd": "1.0.0"}}\n'}, "dependency-change", "warn"),
    ({"src/keys.py": "KEY = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n"}, "secret", "block"),
    ({"src/app.py": "<<<<<<< HEAD\ndef run(x):\n=======\n"}, "conflict-marker", "block"),
    ({"contextweft.toml": "[quality]\ngate = 'off'\n"}, "config-tamper", "warn"),
    ({".github/x/contextweft.toml": "a\n"}, "config-tamper", "warn"),
    ({"pytest.ini": "[pytest]\naddopts = --deselect tests/test_app.py::test_run\n"}, "test-skipped", "block"),
])
def test_rule(tmp_path, change, rule, severity):
    res, rules = gate(tmp_path, change)
    assert (rule, severity) in rules, rules
    assert res.blocked == (severity == "block" or any(s == "block" for _, s in rules))


def test_editing_existing_config_blocks(tmp_path):
    repo = make_repo(tmp_path / "cfg", {**BASE, "contextweft.toml": "[quality]\ngate = 'block'\n"})
    before = gitsnap.snapshot(repo)
    (repo / "contextweft.toml").write_text("[quality]\ngate = 'off'\n")
    res = evaluate_diff(repo, gitsnap.diff(repo, before, gitsnap.snapshot(repo)), test_globs=[],
                        allowed_paths=[], max_changed_lines=600, duplicate_min_lines=6)
    assert res.blocked and res.findings[0].rule == "config-tamper"


def test_assertions_removed_is_warning_when_tests_restructured(tmp_path):
    new = "from app import run\n\n\ndef test_double():\n    assert run(2) == 4\n"
    _, rules = gate(tmp_path, {"tests/test_app.py": new})
    assert ("assertions-removed", "warn") in rules


def test_duplicate_block_detected(tmp_path):
    copy = "def normalize_again(rows):\n" + BASE["src/util.py"].split("\n", 1)[1]
    _, rules = gate(tmp_path, {"src/other.py": copy})
    assert ("duplicate-block", "warn") in rules


def test_scope_and_allowed_paths(tmp_path):
    big = "".join(f"V{i} = {i}\n" for i in range(50))
    _, rules = gate(tmp_path, {"src/big.py": big, "docs/x.md": "hi\n"}, max_changed_lines=20, allowed_paths=["src/**"])
    assert ("scope-size", "warn") in rules and ("out-of-scope", "block") in rules


def test_reformat_churn(tmp_path):
    lines = [f"value_{i} = compute({i})" for i in range(60)]
    repo_files = {"src/big.py": "\n".join(lines) + "\n"}
    repo = make_repo(tmp_path / "c", repo_files)
    before = gitsnap.snapshot(repo)
    (repo / "src/big.py").write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    after = gitsnap.snapshot(repo)
    res = evaluate_diff(repo, gitsnap.diff(repo, before, after), test_globs=[], allowed_paths=[],
                        max_changed_lines=1000, duplicate_min_lines=6)
    assert "reformat-churn" in {f.rule for f in res.findings}


def test_disabled_rules(tmp_path):
    _, rules = gate(tmp_path, {"src/app.py": "def run(x):\n    # TODO later\n    return x * 2\n"}, disabled_rules=["todo"])
    assert rules == set()


def test_parse_diff_handles_plus_plus_content_and_new_files():
    text = ("diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n@@ -0,0 +1,2 @@\n"
            "+++counter\n+--flag\n")
    d = parse_diff(text)["x.py"]
    assert d.status == "A" and d.added == [(1, "++counter"), (2, "--flag")]


def test_same_size_edit_right_after_commit_is_seen(tmp_path):
    # Regression: copying the index with a fresh mtime defeated git's racy-clean check.
    for i in range(5):
        repo = make_repo(tmp_path / f"race{i}", {"a.py": "x = a - b\n"})
        before = gitsnap.snapshot(repo)
        (repo / "a.py").write_text("x = a + b\n")
        assert gitsnap.changed_files(repo, before, gitsnap.snapshot(repo)) == [("M", "a.py")]


def test_snapshot_does_not_touch_user_index(tmp_path):
    repo = make_repo(tmp_path / "s", {"a.py": "1\n"})
    (repo / "a.py").write_text("2\n")
    (repo / "new.py").write_text("3\n")
    from conftest import git
    before_status = git(repo, "status", "--porcelain")
    gitsnap.snapshot(repo)
    assert git(repo, "status", "--porcelain") == before_status
    assert git(repo, "diff", "--cached", "--name-only") == ""
