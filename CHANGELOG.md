# Changelog

## 0.2.0 (unreleased)

### Added
- **Verification loop**: after the primary agent, ContextWeft snapshots the diff (with a temporary
  git index), runs a deterministic **quality gate** and your `[checks]` test/lint commands, and on
  failure **resumes the same agent session** with the findings and the tail of the failing output
  (`claude --resume`, `codex exec resume`). Configured with `repair_rounds`.
- **Quality gate** (`contextweft gate`): 18 rules for known agent failure modes (test tampering and
  special-casing, placeholders, swallowed errors, suppressions, duplicated blocks, scope creep,
  churn, secrets, debug leftovers, dependency changes). Also works as a Claude Code Stop hook
  (`--hook claude`). See `docs/QUALITY_GATE.md`.
- `contextweft.toml` per-repo config and a `contextweft init` template that detects the test command.
- `batch`: sequential tasks, each in a fresh session, with verified history carried over in
  `.contextweft/history.jsonl` (the directory ignores itself in git).
- `bench`: paired baseline-vs-assisted A/B in fresh clones, with a hidden ground-truth command and
  alternating arm order.
- `compare`: median paired differences, bootstrap 95% CI (n ≥ 10) and an exact McNemar test for success.
- Lean agent mode (default): Claude runs with `--strict-mcp-config --disable-slash-commands`.
- `--max-budget-usd`, `--model`, `--reviewer-model`, `--gate`, `--repair-rounds`, `--test`, `--no-lean`.
- `doctor` reports CLI versions, git status, config validity and problems.
- Ruff and mypy are clean. CI runs on Linux/macOS/Windows with Python 3.10–3.13.

### Changed
- Reviewers always run in a **disposable export** and get the diff inline. Previously the Claude
  reviewer was told to read `git diff` but had no tool that could run it.
- Custom reviewers are now allowed, because isolation no longer depends on the adapter.
- `--reviewer` now defaults to `auto`, and `--review-when` (default `never`) alone decides whether
  a review happens.
- Prompts go over stdin for every provider (no argv length limit, no exposure in `ps`).
- Executables are resolved to full paths, so Windows `.cmd` shims (npm-installed `codex`) launch
  without a shell.
- Timeouts kill the whole process tree.
- The Claude `acceptEdits` primary gets the check commands allow-listed. Before this, it couldn't
  run tests at all in `-p` mode.
- Codex parsing: captures `thread_id`; accepts both `agent_message` and `assistant_message`;
  standalone `error` events are non-fatal and `turn.failed` messages are surfaced (redacted).
- Claude parsing: captures `session_id`, reports `error_max_turns` / `error_max_budget_usd` clearly,
  and turns cumulative usage after `--resume` into per-run deltas.

### Fixed
- Missing SubQ `usage` was counted as 0 tokens; it is now `null` (unknown).
- The snapshot index copy kept a fresh mtime, which defeated git's racy-clean check, so a same-size
  edit made within the same second as the last index write was invisible. Fixed and covered by a
  regression test.
- Removed the duplicate, unused `evaluation.compare` and its hard-coded metrics.
- `SUBQ_API_KEY` is no longer inherited by agent processes.
- The gate snapshot is now taken *before* checks run, and files written by the checks themselves
  (`__pycache__`, coverage, build output) are excluded from the agent's diff in later rounds.
- Checks run with a fresh `PYTHONPYCACHEPREFIX`, so Python never runs stale bytecode after a
  same-size edit made within one second (Python's mtime+size `.pyc` check misses those).
- Found by a live Codex smoke test: `run` crashed when printing its result if agent text held
  non-cp1252 characters (e.g. an emoji) and stdout was redirected on Windows, so the run's result
  was lost. Stdout JSON is now ASCII-escaped, and human-readable text never raises on encoding.
- Claude API/auth failures (`is_error` with `subtype: "success"`) now report the CLI's reason
  (e.g. an expired OAuth session) instead of a generic "Claude execution failed".
- `python -m contextweft` works.

### Verified live (Codex CLI 0.153.3)
- `exec --json` output parsing, `exec resume <thread_id> --json -c sandbox_mode=...` keeping the
  same thread, and per-turn (not cumulative) usage after a resume. A full gate → failing check →
  resume repair → verified loop ran end to end; the repair round's input was 89% cache hits.
- Codex has no lean mode: `--ignore-user-config` saves ~17% of a minimal request but drops
  settings runs depend on (`[windows] sandbox = "elevated"`), after which every edit is blocked.
- Codex hooks in `~/.codex` still run and can write files into the target repo; they show up as
  changed files.

## 0.1.0 (prototype)

- Initial CLI with Claude Code, Codex CLI and custom adapters, optional SubQ reranking, an optional
  read-only reviewer, and paired log comparison.
