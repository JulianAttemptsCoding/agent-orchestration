# Custom agent adapter protocol

```sh
contextweft run "task" --provider custom --custom-command "python my_adapter.py"
```

- The command is parsed with `shlex` (no shell). Quote paths that contain spaces.
- The prompt arrives as UTF-8 on **stdin**. The working directory is the target repository, or a
  disposable export of it in reviewer mode.
- Print exactly one JSON object on stdout:

```json
{"text": "Summary of work and how it was verified",
 "usage": {"input_tokens": 1200, "output_tokens": 300, "cached_input_tokens": 800}}
```

- If you can't report usage, leave the fields out. ContextWeft then marks totals as unknown and
  never assumes zero. `cached_input_tokens` is optional and must be a subset of `input_tokens`.
- Exit with a non-zero code, or include `"error": "..."`, to signal failure.
- The adapter handles its own authentication, permissions and execution limits. ContextWeft
  enforces only the wall-clock `--timeout`, and kills the whole process tree when it runs out.
- `SUBQ_API_KEY` is removed from the adapter's environment.

## Repair rounds

Custom adapters have no session to resume. For a repair round ContextWeft sends a **self-contained**
prompt: the task, the engineering rules, then the failing checks and gate findings.

## Reviewer mode

With `--reviewer custom`, the adapter runs in a throwaway export of the result, and the prompt says
READ-ONLY. Anything the adapter writes there is discarded, so it can't modify your repo even if it
ignores the instruction. End the review text with `VERDICT: PASS` or `VERDICT: FAIL`.
