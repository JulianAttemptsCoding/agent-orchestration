"""Per-repository settings from ``contextweft.toml`` (all keys optional)."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 CI only
    import tomli as tomllib

CONFIG_NAME = "contextweft.toml"
GATE_MODES = ("off", "warn", "block")


@dataclass(frozen=True)
class Config:
    # Ground-truth commands run by the orchestrator, never by trust in agent claims.
    test_command: str | None = None
    lint_command: str | None = None
    check_timeout: int = 900
    # Quality gate over the agent's diff.
    gate: str = "block"
    repair_rounds: int = 2
    max_changed_lines: int = 600
    allowed_paths: list[str] = field(default_factory=list)
    test_globs: list[str] = field(default_factory=lambda: [
        "tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*_test.go",
        "**/*.test.*", "**/*.spec.*", "**/__tests__/**", "spec/**",
    ])
    duplicate_min_lines: int = 6
    disabled_rules: list[str] = field(default_factory=list)
    # Files whose *content* is not scanned (e.g. fixtures that contain slop on purpose).
    exclude_paths: list[str] = field(default_factory=list)
    # Agent invocation.
    model: str | None = None
    reviewer_model: str | None = None
    lean: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None
    # Long-horizon memory: verified summaries of earlier runs added to new prompts.
    history_in_prompt: int = 3
    conventions_file: str | None = None

    def validate(self) -> Config:
        if self.gate not in GATE_MODES:
            raise ValueError(f"gate must be one of {', '.join(GATE_MODES)}")
        for name in ("repair_rounds", "history_in_prompt"):
            if not 0 <= getattr(self, name) <= 10:
                raise ValueError(f"{name} must be between 0 and 10")
        if self.max_changed_lines < 1 or self.check_timeout < 1 or self.duplicate_min_lines < 3:
            raise ValueError("max_changed_lines/check_timeout must be positive; duplicate_min_lines >= 3")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError("max_budget_usd must be positive")
        return self

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_SECTIONS = {
    "checks": {"test": "test_command", "lint": "lint_command", "timeout": "check_timeout"},
    "quality": {"gate": "gate", "repair_rounds": "repair_rounds",
                "max_changed_lines": "max_changed_lines", "allowed_paths": "allowed_paths",
                "test_globs": "test_globs", "duplicate_min_lines": "duplicate_min_lines",
                "disable": "disabled_rules", "exclude_paths": "exclude_paths"},
    "agent": {"model": "model", "reviewer_model": "reviewer_model", "lean": "lean",
              "allowed_tools": "allowed_tools", "max_budget_usd": "max_budget_usd"},
    "memory": {"history_in_prompt": "history_in_prompt", "conventions_file": "conventions_file"},
}
_TYPES = {f.name: f.type for f in fields(Config)}


def _check_type(key: str, value: object) -> None:
    expected = _TYPES[key]
    ok = {
        "str | None": isinstance(value, str),
        "int": type(value) is int,
        "str": isinstance(value, str),
        "bool": isinstance(value, bool),
        "float | None": type(value) in (int, float),
        "list[str]": isinstance(value, list) and all(isinstance(v, str) for v in value),
    }.get(str(expected), False)
    if not ok:
        raise ValueError(f"{CONFIG_NAME}: {key} has invalid type {type(value).__name__}")


def load_config(repo: Path) -> Config:
    path = repo / CONFIG_NAME
    if not path.is_file():
        return Config()
    return parse_config(path.read_text(encoding="utf-8"), str(path))


def parse_config(text: str, source: str = CONFIG_NAME) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{source}: invalid TOML ({exc})") from exc
    values: dict[str, Any] = {}
    for section, body in data.items():
        if section not in _SECTIONS or not isinstance(body, dict):
            raise ValueError(f"{CONFIG_NAME}: unknown section [{section}]")
        for key, value in body.items():
            if key not in _SECTIONS[section]:
                raise ValueError(f"{CONFIG_NAME}: unknown key {section}.{key}")
            target = _SECTIONS[section][key]
            _check_type(target, value)
            values[target] = float(value) if target == "max_budget_usd" else value
    return Config(**values).validate()


def detect_test_command(repo: Path) -> str | None:
    """Best guess for `contextweft init`; the user confirms by editing the file."""
    if (repo / "package.json").is_file():
        try:
            scripts = json.loads((repo / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and "test" in scripts:
            return "npm test --silent"
    if (repo / "Cargo.toml").is_file():
        return "cargo test --quiet"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    if any((repo / n).exists() for n in ("pyproject.toml", "setup.py", "pytest.ini", "tests")):
        return "python -m pytest -q"
    return None


TEMPLATE = """\
# ContextWeft settings. Every key is optional; delete what you don't need.

[checks]
# Ground truth. Run by ContextWeft itself after the agent finishes (no shell; shlex-parsed).
{test_line}
# lint = "ruff check ."
timeout = 900

[quality]
gate = "block"            # off | warn | block (block = failing gate triggers repair / fails run)
repair_rounds = 2         # resume the same agent session with concrete findings (cache-friendly)
max_changed_lines = 600   # bigger diffs are flagged as scope creep
# allowed_paths = ["src/**", "tests/**"]   # edits elsewhere fail the gate
# disable = ["todo"]      # rule names to turn off (see docs/QUALITY_GATE.md)
# exclude_paths = ["tests/fixtures/**"]    # content not scanned (deletion/scope rules still apply)

[agent]
lean = true               # skip MCP servers and skills in agent runs (fewer fixed tokens)
# model = "sonnet"
# reviewer_model = "haiku"
# max_budget_usd = 2.0    # Claude only; client-side estimate
# allowed_tools = ["Bash(npm run build *)"]   # test/lint commands above are allowed automatically

[memory]
history_in_prompt = 3     # verified summaries of recent runs given to the next agent
# conventions_file = "CONVENTIONS.md"
"""


def render_template(repo: Path) -> str:
    detected = detect_test_command(repo)
    test_line = f'test = "{detected}"' if detected else '# test = "python -m pytest -q"'
    return TEMPLATE.format(test_line=test_line)
