# Repository Guidelines

## Project Structure & Module Organization

`custom_components/printer_guard/` contains the Home Assistant integration:
`engine.py` orchestrates assessments, `ha_runtime.py` reads printer states and
cameras, `ai_task_adapter.py` and `media.py` handle AI requests and temporary
attachments, and `__init__.py`/`sensor.py` manage lifecycle and diagnostics.
Configuration validation lives in `config.py`; packaged artwork lives in `brand/`.
`tests/` contains unit tests and synthetic fixtures, `scripts/` contains validation
tools, and `examples/configuration.yaml` documents configuration.

## Build, Test, and Development Commands

Use Python 3.14.2+ on a POSIX host. From the repository root:

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check.py
python3 -m unittest discover -s tests -q
```

These install development dependencies, run the standard checks, and run only
unit tests, respectively. Standard checks also validate compilation, metadata,
icons, example configuration, public links, and Git whitespace.

For disposable Home Assistant lifecycle checks, use a separate Python 3.14.10
environment, install `requirements-ha-test.txt`, and run `python scripts/check_ha.py`.
See [CONTRIBUTING.md](CONTRIBUTING.md) for pinned hassfest setup and update/rollback
checks. Local integration setup is documented in [README.md](README.md).

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and variables,
`PascalCase` classes, and `UPPER_CASE` constants. Keep changes focused and follow
surrounding conventions. The standard check script runs no standalone formatter
or linter; Ruff is pinned in the hassfest dependencies.
Do not extract shared helpers, types, or modules before three real uses; propose
abstractions for approval first.

## Testing Guidelines

Use standard-library `unittest`, including `IsolatedAsyncioTestCase` for async
behavior. Name files `tests/test_*.py` and methods `test_*`. No numeric coverage
threshold is configured. Cover relevant configuration rejection, printer
isolation, monitor-only behavior, redaction, cancellation, and cleanup paths.
Use synthetic JPEGs and fake services. Unit results do not establish live-camera
freshness, detection accuracy, or notification delivery.

## Commit & Pull Request Guidelines

Recent commits use imperative subjects, such as `Simplify frame validation and runtime`.
Keep subjects on one line, at most 50 characters. Agents must obtain explicit
authorization before committing. PRs should explain the problem, resulting
behavior, relevant issues, and exact validation performed. Inspect new files
with `git status --short --untracked-files=all` alongside the diff.

## Security & Operational Boundaries

Preserve disabled physical controls. Runtime changes require supervised Home
Assistant validation before deployment. Keep real images, credentials, household
identifiers, prompts, and unredacted logs private. Verify claims against current
files, tests, logs, or authoritative documentation before reporting them.
