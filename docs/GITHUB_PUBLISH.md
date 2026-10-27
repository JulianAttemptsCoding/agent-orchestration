# Publishing

Canonical repository: https://github.com/JulianAttemptsCoding/agent-orchestration

Before pushing:

```sh
python -m pytest -q && python -m ruff check src tests && python -m mypy && python -m build
git status --short          # confirm no secrets, .env files, dist/ or local logs are staged
```

Tag a release only after CI is green:

```sh
git tag v0.2.0
git push origin v0.2.0
gh release create v0.2.0 dist/* --title "v0.2.0" --notes-file CHANGELOG.md
```

PyPI: not published. The name `contextweft-orchestrator` was unclaimed on 2026-10-27. Publishing
needs separate approval and a trusted-publisher setup. Don't upload with a personal token.
Installing the package never creates repositories, commits or pushes.
