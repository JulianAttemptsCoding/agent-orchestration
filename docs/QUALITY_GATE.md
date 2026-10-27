# Why agents write sloppy code, and what ContextWeft does about it

Coding agents fail in a small number of repeatable ways. Most of them are visible in the
diff, so ContextWeft checks the diff with plain code (no model, no tokens) and only then,
optionally, asks a second model.

## Failure modes this targets

| Failure mode | Evidence | What ContextWeft does |
| --- | --- | --- |
| **Gaming the tests**: editing or deleting tests, skipping them, special-casing test inputs, detecting the test environment | ImpossibleBench (Zhong, Raghunathan, Carlini, 2025) turns real tasks into ones that can only be "passed" by cheating; frontier models frequently cheat, and stronger models cheat more often. The strategies it found include editing test files, overloading `__eq__`, keeping extra state, and hard-coding outputs for specific inputs. | `test-deleted`, `assertions-removed`, `test-skipped` (skip/xfail/only/`--deselect`), `trivial-assert`, `test-special-case`, `config-tamper`. Tests are run by the orchestrator, not trusted from the agent's summary. |
| **Duplicating instead of reusing** | GitClear's analysis of 211M changed lines (2020–2024) reports far more duplicated blocks and much less moved (refactored) code as AI assistants spread. It shows correlation, not proven causation. | `duplicate-block`: a new run of 6+ meaningful lines that already exists elsewhere in the repo. The prompt tells the agent to search for and reuse helpers first. |
| **Saying it's done when it isn't**: placeholders, "simplified" versions, stubs, TODOs | Practitioner reports describe an "illusion of completion": work that passes the agent's own checks but fails real validation. We found no controlled study measuring this directly. | `placeholder` (phrases like "in a real implementation", `NotImplementedError`, `todo!()`), `todo`. Status is `verified` only when the configured checks really pass. |
| **Silencing problems**: broad `except: pass`, empty `catch {}`, new `# type: ignore` / `eslint-disable` / `as any` | Common review finding in agent PRs. | `swallowed-error`, `suppression`. |
| **Scope creep and churn**: unrelated refactors, reformatting, huge diffs | Large unfocused diffs are harder to review, and agents often reformat code they didn't need to touch. | `scope-size`, `reformat-churn`, `out-of-scope` (with `allowed_paths`). |
| **Invented or unnecessary dependencies** | Package hallucination ("slopsquatting") is a known supply-chain risk. | `dependency-change` warns when a manifest changes. |
| **Leftovers**: debugger calls, console logging, conflict markers, secrets | — | `debug-leftover`, `conflict-marker`, `secret`. |
| **Context rot on long tasks**: quality drops as the context grows, agents drift and repeat rejected patterns | Chroma's 2025 "Context Rot" study measured worse results on 18 models as input length grew, even on simple tasks. Most write-ups about coding agents are practitioner reports. | `contextweft batch`: every task gets a **fresh** session, and only a few lines of *verified* history (`.contextweft/history.jsonl`) carry over. |

Sources: [ImpossibleBench (arXiv 2510.20270)](https://arxiv.org/abs/2510.20270),
[GitClear coverage (DevClass)](https://devclass.com/2025/02/20/ai-is-eroding-code-quality-states-new-in-depth-report/),
[LeadDev on AI technical debt](https://leaddev.com/technical-direction/how-ai-generated-code-accelerates-technical-debt),
[Context rot overview](https://morphllm.com/context-rot). Secondary sources are marked as such above.
None of the rules has been tested for false-positive rate on a large corpus yet.

## The loop

```text
task -> scout (local ranking, optional SubQ) -> ONE primary agent
     -> snapshot diff (temp git index; your index/HEAD untouched)
     -> quality gate (free) + your test/lint commands (free)
     -> failing?  resume the SAME agent session with concrete findings + test output tail
                  (cached context, so a repair costs much less than a fresh run), up to repair_rounds
     -> optional reviewer (risk-gated): gets the diff inline and runs in a disposable export
     -> status: verified | passed-unverified | failed-verification | agent-failed
```

Why this order: deterministic checks cost no tokens and catch the most common failures, so they
run first. A second model only runs when the task is risky (`--review-when risk`) or you ask for it.
It gets the diff in the prompt, so it doesn't have to spend tokens exploring the repo.

## Rules

| Rule | Severity | Trigger (added/removed lines in the agent's diff) |
| --- | --- | --- |
| `config-tamper` | block / warn | existing `contextweft.toml` edited or deleted (block); new one added (warn) |
| `out-of-scope` | block | file outside `allowed_paths` (when set) |
| `test-deleted` | block | test file deleted |
| `assertions-removed` | block (warn if new tests were added) | net fewer assertions in a test file |
| `test-skipped` | block | skip / xfail / `.only` / `xit` / `t.Skip` / `#[ignore]` / `--deselect` / `--ignore=` |
| `trivial-assert` | warn | `assert True`, `expect(true).toBe(true)` |
| `test-special-case` | block | non-test code reads `PYTEST_CURRENT_TEST`, `JEST_WORKER_ID`, `NODE_ENV === 'test'`, ... |
| `placeholder` | block / warn | "in a real implementation", "simplified version", "placeholder logic" (block); `NotImplementedError`, `todo!()` (warn) |
| `todo` | warn | new TODO / FIXME / XXX / HACK comment |
| `suppression` | warn | `# type: ignore`, `# noqa`, `@ts-ignore`, `eslint-disable`, `#[allow(`, `as any`, ... |
| `swallowed-error` | warn | broad `except` with `pass`/`...`/`continue`; empty `catch {}` |
| `debug-leftover` | block / warn | `breakpoint()`, `pdb.set_trace`, `debugger` (block); `console.log` (warn) |
| `dependency-change` | warn | package.json, pyproject.toml, Cargo.toml, go.mod, ... changed |
| `secret` | block | high-precision credential shapes (`sk-...`, `ghp_...`, `AKIA...`, private key headers) |
| `conflict-marker` | block | `<<<<<<<` / `=======` / `>>>>>>>` |
| `scope-size` | warn | changed lines > `max_changed_lines` |
| `reformat-churn` | warn | at least 40 lines removed and at least half of them re-added unchanged |
| `duplicate-block` | warn | `duplicate_min_lines`+ meaningful lines identical to code elsewhere |

`block` findings fail the gate in `gate = "block"` mode and start a repair round. `warn` findings
go into the repair prompt ("fix unless clearly justified") but don't fail the run on their own.

`contextweft gate` reads its rules from the **base commit's** `contextweft.toml`, so uncommitted edits
(for example ones an agent made) can't loosen the gate that is judging them. If the config isn't
committed yet, plain `gate` uses the working-tree file, and hook mode uses the defaults.

Tuning, all in `contextweft.toml`: `disable = ["todo"]`, `exclude_paths = ["tests/fixtures/**"]`,
`test_globs`, `allowed_paths`, `max_changed_lines`, `duplicate_min_lines`. Agents can't change these
settings, because editing the config file is itself a blocking finding.

## Use the gate without the orchestrator

```sh
contextweft gate                 # judge uncommitted work vs HEAD; exit 1 if blocked
contextweft gate --base main     # judge the whole branch
```

As a **Claude Code Stop hook** (in interactive sessions too), add this to `.claude/settings.json`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "contextweft gate --hook claude"}]}]}}
```

When the gate blocks, the hook exits 2, so Claude gets the findings and keeps working. If Claude
tries to stop again in the same turn (`stop_hook_active`), the hook lets it stop, so it can never
loop forever. Note that the hook judges *all* uncommitted changes, including your own.
