# Release checklist

## Automated (must pass)
- [ ] `python -m pytest -q` (fake Claude/Codex/custom CLIs, real git, no network)
- [ ] `python -m ruff check src tests` and `python -m mypy`
- [ ] `python -m build` produces a wheel and an sdist; the wheel contains `SKILL.md` and `py.typed`
- [ ] Clean venv: `pip install dist/*.whl`, then `contextweft doctor`, `init`, `plan --offline`,
      `run --dry-run`, and `gate` all work
- [ ] CI green on ubuntu/macos/windows × Python 3.10–3.13

## Manual / needs credentials (record results in the CHANGELOG)
- [ ] Live Claude Code smoke test: one tiny task. Confirm the `session_id`/`modelUsage` fields and the
      cumulative-after-resume behaviour that `per_run_usage` assumes.
- [ ] Live Codex smoke test: confirm the `thread.started`/`turn.completed` usage fields and that
      `exec resume <id> --json -c sandbox_mode=workspace-write -` behaves as expected.
- [ ] Live SubQ Snap call with an authorized key (`scout` without `--offline`).
- [ ] `bench` on 30 or more realistic paired tasks before making any claim about savings or quality.
- [ ] Re-check the project and package names (two small unrelated GitHub projects named "ContextWeft"
      exist, one of them about agent context).
- [ ] PyPI publication only with separate approval (`contextweft-orchestrator` was free on 2026-10-27).
