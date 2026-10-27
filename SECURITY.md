# Security and privacy

## Threat model in one paragraph

ContextWeft starts coding-agent CLIs that can read and edit your repository and run commands,
within those CLIs' own permission systems. It is **not a sandbox**. It adds guardrails around the
agents. It does not replace reviewing what they changed. Only use it on repositories you trust,
or work in a disposable clone or worktree.

## What ContextWeft guarantees (and tests)

- No `shell=True` anywhere. Configured commands are parsed with `shlex` and run directly.
- Prompts are sent over stdin, so they never appear in process listings.
- It never passes `--dangerously-skip-permissions`, `bypassPermissions`, or Codex's
  `--dangerously-bypass-approvals-and-sandbox`.
- It never commits, pushes, tags or publishes, and never changes your git index, HEAD, branches or
  stash. Snapshots use a temporary index file.
- Reviewers (Claude, Codex or custom) run in a **disposable export** of the result, which is deleted
  afterwards, so a reviewer can't modify your repository. Claude reviewers also run with
  `--permission-mode plan --tools Read,Grep,Glob`, Codex reviewers with `--sandbox read-only`.
- `SUBQ_API_KEY` is removed from every child process's environment.
- Error snippets and check output are redacted for common credential shapes before they are
  stored or put into prompts. `--record` benchmark rows drop model prose, stderr and test output.
- On timeout the entire process tree is killed (`taskkill /T` on Windows, a process group on POSIX).
- The quality gate blocks credential-like literals and edits to `contextweft.toml`.

## Known limits

- Under `claude -p`, Claude Code runs a project's `.claude/settings.json` hooks and connects
  `.mcp.json` servers **without a trust prompt** (Claude Code's documented behaviour). Lean mode
  (the default) skips MCP servers but not hooks.
- The primary agent has write access by design (`acceptEdits` / `workspace-write`).
- Redaction is pattern-based and can miss secrets. Don't put secrets in task text.
- SubQ (optional) sees the task text and candidate file paths. With `--share-code` it also sees
  bounded excerpts. Use `--offline` for no SubQ traffic. Every external provider has its own
  retention terms.
- `.contextweft/history.jsonl` in your repo stores task text, changed file names and a short agent
  summary. The directory ignores itself in git.

## Reporting

Use GitHub's private vulnerability reporting
(Security → Report a vulnerability) on
https://github.com/JulianAttemptsCoding/agent-orchestration. Please don't post exploit
details in public issues.
