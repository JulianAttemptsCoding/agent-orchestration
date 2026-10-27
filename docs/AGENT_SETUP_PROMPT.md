# Copy-paste prompt for your coding agent

Use this prompt in Claude Code, Codex, or another coding agent **with the extracted ContextWeft repository open**.

---

You are the lead maintainer and release engineer for the open-source Python project in the current directory, ContextWeft. Finish preparing this repository as a publicly installable, reliable, security-conscious multi-agent orchestrator that minimizes **total** token usage across SubQ, Claude Code, Codex CLI, and optional custom backends.

## Goal and scope

1. Read README.md, SECURITY.md, pyproject.toml, source modules, tests, CI, docs and package metadata before editing anything. Preserve a one-primary-agent default; second-agent review is explicitly opt-in or risk-gated. Avoid overhead that defeats token savings.
2. Verify all current APIs against *official* docs. Verify actual SubQ Snap Decisions request/response structure, Claude Code CLI machine-readable output, and Codex `exec --json` event schemas. Never present guessed benchmarks as measured results. Account for cache usage correctly and never silently turn missing usage into zero.
3. Improve code quality, typing, error handling, CLI UX, platform compatibility (macOS/Linux/Windows), privacy defaults, logging redaction, security, documentation, and developer experience. Do **not** introduce `--dangerously-skip-permissions`, `shell=True`, automatic commits/pushes, or silent disclosure of source code to external models. Make custom reviewers enforce read-only mode or clearly block unsafe custom reviewer usage.
4. Test the package, installers, and mock provider adapters. Add integration tests that simulate representative Claude/Codex/SubQ payloads; run a complete clean install, `contextweft doctor`, offline planning, dry runs, and security/failure-path tests. If keys are present, request my permission before making billable live API requests. Use an isolated temporary project for destructive tests.
5. Build distributable Python wheel and source archive; ensure packaging includes skill files, examples, documentation as appropriate. Produce comprehensive quickstart and troubleshooting sections. Keep a changelog and release checklist.
6. Implement and validate A/B benchmark workflow: use reproducible fixtures/worktrees, paired task IDs, same models/config, ground-truth tests, observed total input/output tokens, success, time, and cost if pricing is known. Record unmeasured metrics as unknown. Don't claim that compression improves quality without statistically meaningful evidence.
7. Audit license and branding; clearly state the project is not affiliated with Anthropic, OpenAI, or SubQ. Confirm proposed GitHub repo name doesn't conflict with an existing major project; if it does, recommend an alternative before publishing.

## GitHub publication (requires explicit approval)

8. Check `gh --version`, `gh auth status`, `git status`, remote URL, and repository name availability. Ask me for the exact GitHub owner and desired public repository name if not clear. **Do not publish anything until I approve the owner, name, and public visibility.** Never paste tokens or private keys into chat, logs, source, or CI output.
9. Once approved, create a new public GitHub repository under my authorized account using `gh repo create`, push reviewed source only, configure repository description/topics, check README/CI status and release artifacts, and report the final live link. Do not turn on CI workflows that have privileged tokens or autonomous agent writes unless separately approved. Add a standard issue template and basic CONTRIBUTING/SECURITY guidance if missing.
10. If PyPI publication is requested, check namespace availability and supply a separate release proposal. **Do not publish to PyPI without separate approval.**

## Completion criteria

- `python -m pytest -q` passes, package builds, and a clean environment can install and run it.
- All tested code paths have reported results and documented failure behavior; no live claims without live tests.
- Show an explicit QA table: tested item, result, remaining risk.
- Provide reproducible setup commands, the GitHub URL if published, and exact next steps if credentials or approval are needed.
- Iterate and fix failures until all locally testable checks pass. Prefer an honest, demonstrably working MVP to unverified features.

Begin by inspecting the existing repository and proposing only necessary code changes. Proceed with local development and tests immediately; request confirmation **only before irreversible/public/external account actions or paid inference**.

---
