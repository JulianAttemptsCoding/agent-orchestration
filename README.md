# ContextWeft

**Get more verified software work out of a fixed agent budget, with less sloppy code.**
ContextWeft is a small, auditable wrapper around **Claude Code**, **OpenAI Codex CLI** or your own
agent. It runs **one** primary agent, checks the result with free deterministic tools (a diff
quality gate plus your own tests), and on failure **resumes the same session** with concrete
findings instead of starting over. A second "reviewer" model runs only when the task is risky or
you ask for one.

> **Status: v0.2.0, alpha.** All logic is covered by automated tests against fake agent CLIs.
> No token savings or quality gains have been measured yet, and none are claimed; `contextweft bench`
> exists so you can measure them. Not affiliated with Anthropic, OpenAI or SubQ.

```text
task ─► scout (local ranking, optional SubQ) ─► ONE primary agent (claude | codex | custom)
     ─► diff snapshot (temporary git index; your index/HEAD are never touched)
     ─► quality gate (0 tokens) + your test/lint commands (0 tokens)
     ─► failing? ─► resume SAME session with findings + test output tail (cached context) ×N
     ─► optional read-only reviewer: diff inline, runs in a disposable export
     ─► status: verified | passed-unverified | failed-verification | agent-failed
```

## Why it is built this way

**Goal 1: more work per token budget.**
- *One agent by default.* A second agent is never started by accident.
- *Deterministic checks first.* Tests and the diff gate cost no model tokens. A reviewer model
  only runs when `--review-when risk|always` asks for it.
- *Repair by resuming.* A follow-up in the same session (`claude --resume`, `codex exec resume`)
  reuses the cached context. Starting a new agent would pay to rebuild that context.
- *Reviewer gets the diff inline* (capped at 24k characters), so it doesn't have to explore the repo.
- *Lean agent runs* (default): `--strict-mcp-config --disable-slash-commands` for Claude, so MCP tool
  schemas and skill listings aren't re-sent on every turn. Turn this off with `--no-lean`.
- *Fresh sessions for long work.* `contextweft batch` runs each task in a new session, which
  avoids paying again and again for an ever-growing context. A few lines of verified history carry
  continuity from one task to the next.
- *Small pointer list.* The scout suggests up to 8 file paths. It never pastes whole files into
  the prompt.
- Hard caps: `--max-turns`, `--timeout` (kills the whole process tree), `--max-budget-usd` (Claude),
  and `--max-observed-tokens` (no further repair or review after this many tokens).

**Goal 2: better code on medium and long projects.** Agents fail in known ways: gaming tests,
duplicating code, leaving placeholders, silencing errors, scope creep, drifting on long tasks. The
gate checks for each of these. See [docs/QUALITY_GATE.md](docs/QUALITY_GATE.md) for the research
and the full rule table. A short rule list (about 150 tokens) is added to every prompt, so the agent
knows its work will be checked.

## Install

Requires Python 3.10+ and git. To run agents you also need an installed, logged-in `claude` and/or
`codex` CLI.

```sh
git clone https://github.com/JulianAttemptsCoding/agent-orchestration.git
cd agent-orchestration
python -m pip install .
contextweft doctor
```

Or install a built wheel: `python -m pip install dist/contextweft_orchestrator-0.2.0-py3-none-any.whl`.

## Quickstart

```sh
cd /path/to/your/project            # must be a git repo for the diff gate
contextweft init                    # writes contextweft.toml; detects pytest/npm/cargo/go test
contextweft doctor                  # checks local tools and config (runs no model)

contextweft plan "Fix the login redirect bug" --offline        # see the prompt; runs no agent
contextweft run  "Fix the login redirect bug" --provider claude
contextweft run  "Fix authorization checks" --provider codex --review-when risk --reviewer claude
contextweft batch TODO.md --provider claude                    # one task per line or "- [ ] item"
contextweft gate                                               # gate your own uncommitted work
```

`run` prints JSON with `status`, `changed_files`, `gate.findings`, `checks`, `review`, the
per-invocation `runs`, and `totals` (tokens and cost). It exits 0 only when `success` is true.
ContextWeft **never commits, pushes or publishes**. Review the diff yourself.

### `contextweft.toml`

```toml
[checks]
test = "python -m pytest -q"     # ground truth, run by ContextWeft (no shell; shlex-parsed)
lint = "ruff check ."

[quality]
gate = "block"                   # off | warn | block
repair_rounds = 2
max_changed_lines = 600
# allowed_paths = ["src/**", "tests/**"]
# disable = ["todo"]
# exclude_paths = ["tests/fixtures/**"]

[agent]
lean = true
# model = "sonnet"
# reviewer_model = "haiku"       # a cheaper reviewer
# max_budget_usd = 2.0
# allowed_tools = ["Bash(npm run build *)"]   # check commands are allowed automatically

[memory]
history_in_prompt = 3
# conventions_file = "CONVENTIONS.md"         # first 2000 chars go into the prompt
```

Claude's `acceptEdits` mode does not run shell commands that aren't allow-listed. ContextWeft
therefore allow-lists exactly your `test`/`lint` commands (`Bash(<cmd>)`, `Bash(<cmd> *)`), so the
agent can run them, and then runs them again itself as the ground truth.

## Commands

| Command | What it does | Model tokens |
| --- | --- | --- |
| `doctor` | tool versions, git/config checks | 0 |
| `init` | write a `contextweft.toml` template | 0 |
| `gate [--base REF] [--hook claude]` | diff quality gate on uncommitted work; also works as a Claude Code Stop hook | 0 |
| `scout TASK` | rank likely-relevant files (local; SubQ optional) | 0 (SubQ: input only) |
| `plan TASK` | show the full prompt and plan | 0 (SubQ optional) |
| `run TASK` | agent + gate + checks + repair (+ optional review) | agent |
| `batch FILE` | tasks in order, a fresh session each, stops at the first failure (`--keep-going`) | agent |
| `bench CASES --out DIR` | paired baseline-vs-assisted A/B in fresh clones | agent |
| `compare --baseline A --assisted B` | paired statistics over recorded runs | 0 |
| `eval DATASET` | scout recall@k against gold files | 0 (SubQ optional) |
| `install` | advisory `SKILL.md` for Claude Code and Codex, plus a hook snippet | 0 |

## Token accounting (read this before trusting numbers)

- `totals.input_tokens` = all agent input (Claude cache reads and cache writes included) + SubQ input.
  Codex `cached_input_tokens` are a subset of its input, so they are not added twice.
- After `--resume`, Claude reports **whole-conversation** totals. ContextWeft converts these into
  per-invocation deltas. If the numbers don't increase monotonically, the raw values are kept.
- Missing usage is reported as `null` ("unknown"), **never 0**. Totals that depend on it are `null` too.
- `agent_cost_usd` is the CLI's own client-side estimate (Claude only), not your bill.

## Measuring it (A/B)

```jsonl
{"id": "bug-1", "task": "Fix the off-by-one in pagination", "test": "python -m pytest -q tests/test_page.py"}
```

```sh
contextweft bench cases.jsonl --repo . --out results/ --provider claude --model sonnet
```

Each case runs in two **fresh clones** at the same ref, with the same provider, model and limits:
- **baseline**: the bare task, no scout, no gate, no repair (a plain single agent call);
- **assisted**: the full ContextWeft loop.

The `test` command is the ground truth. The agent never sees it, and the harness runs it after
the agent exits. The order of the two arms alternates between cases. `compare` reports means,
median paired differences, bootstrap 95% CIs (with at least 10 cases) and an exact McNemar test on
success. Fewer than 10 paired cases → no inference is reported. Treat any result as provisional
until you have 30 or more realistic tasks.

## Privacy and security

- Prompts go to agents over **stdin**, never on the command line (where `ps` could show them).
  Nothing runs with `shell=True`. On timeout the whole process tree is killed, including
  grandchildren behind Windows `.cmd` shims.
- `SUBQ_API_KEY` is removed from the agents' environment. Error snippets and check output are
  redacted for common credential shapes. Benchmark records drop model prose, stderr and test output.
- SubQ (optional) gets only the task text and candidate *paths* unless you pass `--share-code`.
  Use `--offline` for zero SubQ traffic.
- Reviewers always run in a **throwaway export** of the result, so any reviewer, custom adapters
  included, can't modify your repo. Claude reviewers also get `--permission-mode plan --tools Read,Grep,Glob`;
  Codex reviewers get `--sandbox read-only`.
- `claude -p` runs a project's hooks and `.mcp.json` servers without a trust prompt. Lean mode skips
  MCP servers, but project hooks still run. Only point ContextWeft at repositories you trust.
- ContextWeft never uses `--dangerously-skip-permissions` or Codex's
  `--dangerously-bypass-approvals-and-sandbox`.

More in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `status: agent-failed`, error mentions login/auth | Run `claude` or `codex login` interactively once. ContextWeft uses the CLI's own login. |
| `Claude hit --max-turns` | Raise `--max-turns` (default 40) or split the task (`batch`). |
| `status: passed-unverified` | No `[checks] test` is configured. Run `contextweft init`. |
| `diff gate skipped: not a git repository` | `git init` and commit a baseline. |
| Codex refuses to run outside a git repo | Codex's own safety check for primary runs; `git init` first. |
| The agent can't run your tests | Put them in `[checks]` (they are allow-listed automatically) or add `[agent] allowed_tools`. |
| Gate flags fixture files that contain slop on purpose | `exclude_paths = ["tests/fixtures/**"]` (deletion and scope rules still apply). |
| A false positive on a rule | `disable = ["rule-name"]` in `[quality]`. Please also report it. |
| Windows: `command not found` for a check | Use the full command (e.g. `python -m pytest`). There is no shell, so built-ins like `dir` won't work. |
| SubQ `fallback` status | Key not set, invalid or out of credits (the error code is shown). Local ranking is used instead. |

## Custom agents

`--provider custom --custom-command "python my_adapter.py"`. The adapter reads the prompt on stdin
and prints `{"text": "...", "usage": {"input_tokens": N, "output_tokens": M}}`.
See [docs/CUSTOM_ADAPTER.md](docs/CUSTOM_ADAPTER.md).

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest -q          # ~90 tests, fake agent CLIs, no network
python -m ruff check src tests
python -m mypy
python -m build
```

CI runs Linux, macOS and Windows on Python 3.10–3.13. See [CONTRIBUTING.md](CONTRIBUTING.md),
[CHANGELOG.md](CHANGELOG.md) and [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## References

[Claude Code CLI](https://code.claude.com/docs/en/cli-reference) ·
[Claude Code headless](https://code.claude.com/docs/en/headless) ·
[Codex non-interactive](https://developers.openai.com/codex/non-interactive) ·
[SubQ Decisions API](https://docs.subq.ai/endpoints/decisions-api/)

License: MIT. "Claude" is a trademark of Anthropic; "Codex" and "OpenAI" are trademarks of OpenAI.
This project is independent and is not affiliated with or endorsed by Anthropic, OpenAI or SubQ.
