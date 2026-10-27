---
name: contextweft
description: Check your own diff for test tampering, placeholders, duplicated code and other slop before declaring a coding task done; optionally rank likely-relevant files. Use before finishing any code change in this repo.
---

Before you say a code change is finished, run `contextweft gate` from the repo root. It costs no
model tokens. It exits 1 and lists findings when the diff contains blocking problems: deleted,
skipped or weakened tests, code that checks for the test environment, placeholder or "simplified"
implementations, debugger calls, leaked secrets, or edits to `contextweft.toml`. Fix the code
itself. Never silence a finding by editing tests or the config. `[warn]` findings (TODOs,
suppressions, swallowed errors, duplicated blocks, big diffs) should be fixed unless there is a
clear reason, and if you keep one, say why.

To find where to start: `contextweft scout "TASK" --offline` gives an advisory list of file paths
(not exhaustive; read further when you need to).

Don't launch `contextweft run` from inside an agent session unless the user asks for it, because
it starts another coding agent. Never commit, push or publish on the user's behalf.
