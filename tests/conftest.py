"""Shared fixtures: throwaway git repos and fake agent CLIs (no network, no inference)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_AGENT = r'''
import json, os, sys, time
provider = sys.argv[1]
argv = sys.argv[2:]
prompt = sys.stdin.read()
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "scenario.json"), encoding="utf-8") as f:
    steps = json.load(f)
counter = os.path.join(state, "count")
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
step = steps[min(n, len(steps) - 1)]
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"provider": provider, "argv": argv, "prompt": prompt, "cwd": os.getcwd(),
                        "subq_key_visible": "SUBQ_API_KEY" in os.environ}) + "\n")
time.sleep(step.get("sleep", 0))
for rel, content in step.get("write", {}).items():
    os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
    if content is None:
        os.remove(rel)
    else:
        open(rel, "w", encoding="utf-8").write(content)
text = step.get("text", "Changed files and ran tests.")
u = step.get("usage", {"input": 100, "output": 10, "cached": 40})
if provider == "claude":
    out = {"type": "result", "subtype": step.get("subtype", "success"), "is_error": step.get("is_error", False),
           "result": text, "session_id": step.get("session", "sess-1"), "total_cost_usd": step.get("cost", 0.01),
           "modelUsage": {"m": {"inputTokens": u["input"], "cacheReadInputTokens": u["cached"],
                                "cacheCreationInputTokens": 0, "outputTokens": u["output"]}}}
    print(json.dumps(out))
elif provider == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": step.get("session", "thread-1")}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}))
    if step.get("fail"):
        print(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
    else:
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": u["input"],
              "cached_input_tokens": u["cached"], "output_tokens": u["output"]}}))
else:
    print(json.dumps({"text": text, "usage": {"input_tokens": u["input"], "output_tokens": u["output"]}}))
sys.exit(step.get("exit", 0))
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def make_repo(root: Path, files: dict[str, str], commit: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
    return root


BUGGY = {
    "src/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
                          "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    "contextweft.toml": f'[checks]\ntest = "{Path(sys.executable).as_posix()} -m pytest -q -p no:cacheprovider tests"\n',
}
FIXED_CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def buggy_repo(tmp_path):
    return make_repo(tmp_path / "proj", BUGGY)


class FakeAgents:
    def __init__(self, base: Path, monkeypatch):
        self.bin = base / "fakebin"
        self.state = base / "fakestate"
        self.bin.mkdir()
        self.state.mkdir()
        script = self.bin / "fake_agent.py"
        script.write_text(FAKE_AGENT, encoding="utf-8")
        for name in ("claude", "codex"):
            if os.name == "nt":
                (self.bin / f"{name}.cmd").write_text(
                    f'@"{sys.executable}" "{script}" {name} %*\r\n', encoding="utf-8")
            else:
                exe = self.bin / name
                exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {name} "$@"\n')
                exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("FAKE_STATE", str(self.state))
        self.custom = f'"{sys.executable}" "{script}" custom'

    def scenario(self, steps: list[dict]) -> None:
        (self.state / "scenario.json").write_text(json.dumps(steps), encoding="utf-8")

    def calls(self) -> list[dict]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def agents(tmp_path, monkeypatch):
    return FakeAgents(tmp_path, monkeypatch)
