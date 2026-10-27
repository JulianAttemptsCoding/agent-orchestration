# Contributing

```sh
python -m pip install -e ".[dev]"
python -m pytest -q && python -m ruff check src tests && python -m mypy
```

Ground rules:
- Changes to provider parsing need fixture tests based on documented CLI/API output. Missing token
  usage must stay `None`; never default it to zero.
- New gate rules need a positive test and a no-false-positive check on realistic code. State which
  documented failure mode the rule targets.
- No automatic commits, pushes, publishing or permission-bypass flags. No `shell=True`.
- Before claiming an optimization saves tokens or improves quality, show paired `contextweft bench`
  results (same models, fresh clones, ground-truth tests) with success reported next to usage.
- Keep prompt text short. Every line is paid for on every agent turn.

ContextWeft dogfoods itself: this repo's `contextweft.toml` runs the test suite and ruff as checks.
