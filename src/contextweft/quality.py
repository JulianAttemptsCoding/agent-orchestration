"""Deterministic diff gate for known LLM-agent failure modes. Costs zero model tokens.

Each rule targets a documented pattern: test tampering and special-casing
(ImpossibleBench), duplicated instead of reused code (GitClear), placeholder or
stubbed "done" work, silenced errors/linters, scope creep, and leaked secrets.
Rules are heuristics over *added/removed lines*; they flag, they do not prove.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .procs import SECRET_PATTERNS
from .repository import list_files, read_sample

BLOCK, WARN = "block", "warn"

CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
                 ".kt", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".scala"}
MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "cargo.toml",
             "go.mod", "gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json"}
PROTECTED_FILES = {"contextweft.toml"}

ASSERT_RE = re.compile(r"\bassert\b|\bexpect\s*\(|\bassert[A-Z_]\w*\s*\(|\.should\b|"
                       r"\bt\.(?:Error|Errorf|Fatal|Fatalf)\(|\bassert(?:_eq|_ne)?!\(|\brequire\.\w+\(")
TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_|^\s*(?:it|test)\s*\(|^\s*func\s+Test\w*\(|"
                         r"#\[test\]|@Test\b")
SKIP_RE = re.compile(r"@pytest\.mark\.(?:skip|xfail)|\bpytest\.(?:skip|xfail)\(|@unittest\.skip|"
                     r"\b(?:it|test|describe)\.(?:skip|only|todo)\s*\(|\bx(?:it|describe|test)\s*\(|"
                     r"\bfit\s*\(|\bt\.Skip(?:f|Now)?\(|#\[ignore\]|@Disabled\b|@Ignore\b|"
                     r"--deselect\b|--ignore(?:-glob)?=")
TRIVIAL_ASSERT_RE = re.compile(r"\bassert\s+(?:True|1|not\s+False)\s*(?:#.*)?$|"
                               r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assertTrue\(\s*True\s*\)")
SPECIAL_CASE_RE = re.compile(r"PYTEST_CURRENT_TEST|[\"']pytest[\"']\s+in\s+sys\.modules|"
                             r"JEST_WORKER_ID|VITEST\b|NODE_ENV\s*[!=]==?\s*[\"']test[\"']|"
                             r"\bif\s+.*\b(?:is_test|IS_TEST|TESTING)\b")
SUPPRESS_RE = re.compile(r"#\s*type:\s*ignore|#\s*noqa|#\s*pylint:\s*disable|#\s*pyright:\s*ignore|"
                         r"#\s*nosec|@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable|"
                         r"#\[allow\(|@SuppressWarnings|//\s*nolint|\bas\s+any\b")
BARE_EXCEPT_RE = re.compile(r"^\s*except\s*(?:(?:Base)?Exception\b[^:]*)?:\s*(?P<body>.*)$")
SWALLOW_BODY_RE = re.compile(r"^\s*(?:pass|\.\.\.|continue|return(?:\s+None)?)\s*(?:#.*)?$")
EMPTY_CATCH_RE = re.compile(r"\bcatch\s*(?:\([^)]*\))?\s*\{\s*(?://.*)?\}|\.catch\(\s*\(\s*\w*\s*\)\s*=>\s*\{\s*\}\s*\)")
TODO_RE = re.compile(r"(?:#|//|/\*|\*|--)\s*(?:TODO|FIXME|XXX|HACK)\b")
STUB_RE = re.compile(r"raise\s+NotImplementedError|\b(?:todo|unimplemented)!\(|"
                     r"throw\s+new\s+\w*Error\(\s*[\"'`]not\s+implemented", re.I)
SLOP_PHRASE_RE = re.compile(r"in a real (?:implementation|application|system|world)|"
                            r"in production,? you (?:would|should)|simplified (?:version|implementation)|"
                            r"placeholder (?:implementation|logic|value)|mock implementation|"
                            r"for (?:now|simplicity),? (?:we|just|this|I)", re.I)
DEBUG_BLOCK_RE = re.compile(r"\bbreakpoint\(\)|\bpdb\.set_trace\(|^\s*debugger\s*;?\s*$|binding\.pry|\bdbg!\(")
CONSOLE_RE = re.compile(r"\bconsole\.(?:log|debug)\(")
CONFLICT_RE = re.compile(r"^(?:<{7}|>{7}|={7})(?: |$)")
TRIVIAL_LINE_RE = re.compile(r"^[\s{}()\[\];,:.]*$|^(?:return|pass|else:?|end|break|continue|"
                             r"try:|finally:|\}\s*else\s*\{)\s*;?$")
IMPORT_RE = re.compile(r"^(?:import|from|#include|use|using|require|package)\b")
HIGH_PRECISION_SECRETS = SECRET_PATTERNS[:6]


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int | None
    message: str

    def to_json(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.severity}] {self.rule} {loc}: {self.message}"


@dataclass
class FileDiff:
    path: str
    status: str = "M"
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class GateResult:
    findings: list[Finding]
    blocked: bool
    files_changed: int
    lines_added: int
    lines_removed: int

    def to_json(self) -> dict:
        return {"blocked": self.blocked, "files_changed": self.files_changed,
                "lines_added": self.lines_added, "lines_removed": self.lines_removed,
                "findings": [f.to_json() for f in self.findings]}


def parse_diff(text: str) -> dict[str, FileDiff]:
    """Parse ``git diff --unified=N`` output into per-file added/removed lines."""
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    old_path = ""
    in_hunk = False
    new_line = old_line = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current, in_hunk, old_path = None, False, ""
            continue
        if not in_hunk:
            if raw.startswith("--- "):
                old_path = raw[4:].removeprefix("a/")
            elif raw.startswith("+++ "):
                new_path = raw[4:].removeprefix("b/")
                path = old_path if new_path == "/dev/null" else new_path
                status = "A" if old_path == "/dev/null" else "D" if new_path == "/dev/null" else "M"
                current = files.setdefault(path, FileDiff(path, status))
            elif raw.startswith("rename to "):
                files.setdefault(raw[10:], FileDiff(raw[10:], "R"))
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m and current is not None:
                old_line, new_line = int(m.group(1)), int(m.group(2))
                in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+"):
            current.added.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.removed.append((old_line, raw[1:]))
            old_line += 1
        elif raw.startswith(" "):
            new_line += 1
            old_line += 1
    return files


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch(path, g) or (g.startswith("**/") and fnmatch(path, g[3:])) for g in globs)


def _normalize(line: str) -> str:
    return " ".join(line.split())


def _meaningful(line: str) -> bool:
    n = _normalize(line)
    return len(n) >= 4 and not TRIVIAL_LINE_RE.match(n) and not IMPORT_RE.match(n) \
        and not n.startswith(("#", "//", "*", "/*"))


def _window_hashes(lines: list[tuple[int, str]], size: int) -> list[tuple[str, int, int]]:
    """(hash, first_line, last_line) for each window of ``size`` meaningful lines."""
    kept = [(n, _normalize(t)) for n, t in lines if _meaningful(t)]
    out = []
    for i in range(len(kept) - size + 1):
        chunk = "\n".join(t for _, t in kept[i:i + size])
        out.append((hashlib.blake2b(chunk.encode(), digest_size=12).hexdigest(),
                    kept[i][0], kept[i + size - 1][0]))
    return out


def _contiguous_runs(added: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    runs: list[list[tuple[int, str]]] = []
    for n, t in added:
        if runs and runs[-1][-1][0] == n - 1:
            runs[-1].append((n, t))
        else:
            runs.append([(n, t)])
    return runs


def find_duplicates(repo: Path, diffs: dict[str, FileDiff], size: int,
                    max_files: int = 4000) -> list[Finding]:
    runs = [(d.path, run) for d in diffs.values() if Path(d.path).suffix.lower() in CODE_SUFFIXES
            for run in _contiguous_runs(d.added) if len(run) >= size]
    if not runs:
        return []
    wanted = {h: (p, a, b) for p, run in runs for h, a, b in _window_hashes(run, size)}
    index: dict[str, list[tuple[str, int]]] = {}
    for rel in list_files(repo)[:max_files]:
        if Path(rel).suffix.lower() not in CODE_SUFFIXES:
            continue
        content = read_sample(repo, rel)
        numbered = list(enumerate(content.splitlines(), 1))
        for h, a, _ in _window_hashes(numbered, size):
            if h in wanted:
                index.setdefault(h, []).append((rel, a))
    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()
    for h, (path, a, b) in wanted.items():
        others = [(p, n) for p, n in index.get(h, []) if not (p == path and a <= n <= b)]
        run_key = (path, a // 50)
        if others and run_key not in reported:
            reported.add(run_key)
            p, n = others[0]
            findings.append(Finding("duplicate-block", WARN, path, a,
                                    f"{size}+ lines duplicate {p}:{n}; reuse or extract a shared helper"))
    return findings


def evaluate_diff(repo: Path, diff_text: str, *, test_globs: list[str], allowed_paths: list[str],
                  max_changed_lines: int, duplicate_min_lines: int,
                  disabled_rules: frozenset[str] = frozenset(),
                  exclude_paths: list[str] | None = None) -> GateResult:
    diffs = parse_diff(diff_text)
    out: list[Finding] = []

    def add(rule: str, severity: str, path: str, line: int | None, message: str) -> None:
        if rule not in disabled_rules:
            out.append(Finding(rule, severity, path, line, message))

    for d in diffs.values():
        is_test = matches_any(d.path, test_globs)
        name = Path(d.path).name.lower()
        is_code = Path(d.path).suffix.lower() in CODE_SUFFIXES
        if name in PROTECTED_FILES:
            if d.status == "A":
                add("config-tamper", WARN, d.path, None, "new orchestrator config; a human should review it")
            else:
                add("config-tamper", BLOCK, d.path, None, "agents must not edit the orchestrator config")
        if allowed_paths and not matches_any(d.path, allowed_paths):
            add("out-of-scope", BLOCK, d.path, None, "file is outside allowed_paths")
        if is_test and d.status == "D":
            add("test-deleted", BLOCK, d.path, None, "test file deleted")
        if name in MANIFESTS and d.added:
            add("dependency-change", WARN, d.path, d.added[0][0],
                "manifest changed; confirm every new dependency exists and is required")
        if is_test and d.status != "D":
            removed = sum(1 for _, t in d.removed if ASSERT_RE.search(t))
            added = sum(1 for _, t in d.added if ASSERT_RE.search(t))
            new_tests = any(TEST_DEF_RE.search(t) for _, t in d.added)
            if removed > added:
                add("assertions-removed", WARN if new_tests else BLOCK, d.path,
                    d.removed[0][0] if d.removed else None,
                    f"net {removed - added} assertion(s) removed; do not weaken tests to pass")
        if exclude_paths and matches_any(d.path, exclude_paths):
            continue  # file-level rules above still apply
        prev, prev_n = "", -1
        for n, text in d.added:
            if CONFLICT_RE.match(text):
                add("conflict-marker", BLOCK, d.path, n, "merge conflict marker")
            if any(p.search(text) for p in HIGH_PRECISION_SECRETS):
                add("secret", BLOCK, d.path, n, "credential-like literal added")
            if SKIP_RE.search(text):
                add("test-skipped", BLOCK, d.path, n, "test skipped/focused/deselected; fix code instead")
            if is_test and TRIVIAL_ASSERT_RE.search(text):
                add("trivial-assert", WARN, d.path, n, "assertion cannot fail")
            if is_code and not is_test and SPECIAL_CASE_RE.search(text):
                add("test-special-case", BLOCK, d.path, n, "production code branches on test environment")
            if is_code and SUPPRESS_RE.search(text):
                add("suppression", WARN, d.path, n, "new lint/type suppression; fix the underlying issue")
            if is_code and SLOP_PHRASE_RE.search(text):
                add("placeholder", BLOCK, d.path, n, "placeholder/simplified implementation marker")
            elif is_code and STUB_RE.search(text):
                add("placeholder", WARN, d.path, n, "unimplemented stub added")
            elif is_code and TODO_RE.search(text):
                add("todo", WARN, d.path, n, "new TODO/FIXME left in code")
            if is_code and not is_test and DEBUG_BLOCK_RE.search(text):
                add("debug-leftover", BLOCK, d.path, n, "debugger statement left in code")
            elif is_code and not is_test and CONSOLE_RE.search(text):
                add("debug-leftover", WARN, d.path, n, "console logging added")
            if is_code:
                here = BARE_EXCEPT_RE.match(text)
                before = BARE_EXCEPT_RE.match(prev) if prev_n == n - 1 else None
                one_liner = here is not None and SWALLOW_BODY_RE.match(here.group("body"))
                two_liner = before is not None and not before.group("body") and SWALLOW_BODY_RE.match(text)
                if one_liner or two_liner or EMPTY_CATCH_RE.search(text):
                    add("swallowed-error", WARN, d.path, n, "broad exception silently ignored")
            prev, prev_n = text, n

    added_total = sum(len(d.added) for d in diffs.values())
    removed_total = sum(len(d.removed) for d in diffs.values())
    if added_total + removed_total > max_changed_lines:
        add("scope-size", WARN, "(diff)", None,
            f"{added_total + removed_total} changed lines > {max_changed_lines}; keep changes focused")
    removed_lines = Counter(_normalize(t) for d in diffs.values() for _, t in d.removed if t.strip())
    added_lines = Counter(_normalize(t) for d in diffs.values() for _, t in d.added if t.strip())
    readded = sum((removed_lines & added_lines).values())
    if removed_total >= 40 and readded >= 0.5 * removed_total:
        add("reformat-churn", WARN, "(diff)", None,
            f"{readded} lines removed and re-added unchanged; avoid unrelated reformatting/moves")
    if "duplicate-block" not in disabled_rules:
        scanned = {p: d for p, d in diffs.items() if not (exclude_paths and matches_any(p, exclude_paths))}
        out.extend(find_duplicates(repo, scanned, duplicate_min_lines))
    blocked = any(f.severity == BLOCK for f in out)
    return GateResult(out, blocked, len(diffs), added_total, removed_total)


def render_findings(findings: list[Finding], limit_chars: int = 2500, limit_items: int = 25) -> str:
    ordered = sorted(findings, key=lambda f: (f.severity != BLOCK, f.path, f.line or 0))
    lines, used = [], 0
    for f in ordered[:limit_items]:
        s = "- " + f.render()
        if used + len(s) > limit_chars:
            break
        lines.append(s)
        used += len(s) + 1
    hidden = len(findings) - len(lines)
    return "\n".join(lines) + (f"\n- ... {hidden} more" if hidden > 0 else "")
