# Contributing

Keep changes focused and preserve disabled physical controls. See the
[README](README.md) for installation and printer requirements.

## Code map

The integration lives in `custom_components/printer_guard/`:

- `config.py`: configuration validation.
- `ha_runtime.py`: printer states and camera access.
- `engine.py` and `frame_validation.py`: assessment orchestration and frame checks.
- `ai_task_adapter.py` and `media.py`: AI requests, temporary attachments and cleanup.
- `__init__.py` and `sensor.py`: lifecycle, scheduling, notifications and diagnostics.

Use synthetic fixtures in `tests/`; validation tools live in `scripts/`.
Follow surrounding code conventions and avoid shared abstractions until there
are at least three real uses.

## Validation

Use Python 3.14.2+ on a POSIX host:

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check.py
```

This runs unit tests, compilation, metadata and icon checks, example configuration
validation, local documentation-link checks, and Git whitespace checks. To run
only unit tests, use `python3 -m unittest discover -s tests -q`.

Cover configuration rejection, printer isolation, monitor-only behavior,
redaction, cancellation and cleanup as relevant.

### Hassfest

In a separate environment, install `requirements-hassfest.txt` and obtain the
Home Assistant Core 2026.10.4 source at commit
`357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b`. Run:

```sh
python3 scripts/check.py --hassfest-core /path/to/core
```

The check requires that exact revision and installed Core version.

### Home Assistant lifecycle

Use a separate Python 3.14.10 environment:

```sh
python -m pip install -r requirements-ha-test.txt
python scripts/check_ha.py --report /tmp/core-results.json
```

This uses Core 2026.11.0 with temporary configuration and synthetic services.
Add `--previous /path/to/previous/custom_components` for cold-process install,
update and rollback checks; otherwise those checks are skipped. Dependency
overlays are rejected, and worker shutdown failures fail the check.

The documented minimum remains Core 2026.10.4; its earlier local lifecycle run
reported an `orjson` shutdown crash ([upstream issue](https://github.com/home-assistant/core/issues/185580)).

Local tests do not establish live-camera freshness, detection accuracy or actual
notification delivery. Runtime changes require supervised Home Assistant
validation before deployment: check camera and printer states, AI/media routing,
model output, notifications, lifecycle and restart recovery. Record exact
validation performed and distinguish local results from remote CI results.

## Submitting changes

Inspect `git diff`, `git status --short --untracked-files=all`, and every new file.
Describe the problem, resulting behavior, relevant issues and validation in PRs.
Use imperative commit subjects of at most 50 characters; agents need explicit
authorization before committing.

Keep real images, prompts, credentials, household identifiers and unredacted logs
private. Use fictitious configuration and synthetic images in fixtures; share
only fixed diagnostic codes and versions.

Contributions must identify copied or adapted material and retain required
license and NOTICE text. See the [Apache 2.0 license](LICENSE).
