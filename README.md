# Printer Guard

Alert-only Home Assistant integration for 3D printers. AI screens
one camera frame; if suspicious, it compares two more about 30 seconds apart. It
alerts only if all three frames are flagged.

Checks start after a two-minute delay and run every minute. After a successful
notify action, repeats for that printer and job are suppressed for 30 minutes.
New jobs don't inherit the cooldown; Home Assistant restarts clear it.

## Setup

Configure a JPEG camera per printer, an image-capable `ai_task` returning
structured data, and a `notify.<service>` accepting `title` and `message`. The
provider receives frames and telemetry and may charge. Check retention, training,
access, and billing terms. Frame narrowly; protect real images, credentials, and
logs.

1. Copy [`custom_components/printer_guard`](custom_components/printer_guard) into
   Home Assistant's `custom_components/` folder.
2. Set `homeassistant.media_dirs.local` to an existing, writable absolute path
   on a local POSIX filesystem. Restart Home Assistant after installing.
3. Choose either method for each printer (at most two printers total):
   - UI: Settings → Devices & services → Add integration → Printer Guard.
     Add one entry per printer. Use its **Configure** action to edit later.
   - YAML: merge the [YAML example](examples/configuration.yaml), validate the
     configuration, and restart Home Assistant. YAML remains supported and is
     never imported, removed or rewritten automatically.

`ai_task` handles comparison; optional `screen_ai_task` handles the initial frame
and defaults to `ai_task` only when omitted. Both need image and structured-data
support. A configured but unavailable screen task does not silently fall back.
UI setup checks local entities, capabilities, notification action registration and
media configuration without capturing images, calling AI or sending test alerts.
See [configuration and ownership](docs/configuration.md) for edits, conflicts,
switching methods and removal.

Select each printer entity directly in the UI or YAML. Names do not need a
shared prefix. The camera must be available, and the required entities must
report these states for a check to run:

| Binding | State |
| --- | --- |
| `print_status` | `running` |
| `current_stage` | `printing` |
| `online` | `on` |
| `print_error` | `off` |
| `task_name` | Current job name |
| `start_time` | Current job start time |

Optional telemetry entities are `current_layer`, `total_layer_count`,
`print_progress`, `nozzle_temperature`, and `bed_temperature`. Omit any you do not
have. Existing prefix-based YAML still works; existing UI entries are upgraded
automatically while keeping their diagnostic identity.

Inspect each sensor's `assessment`, `last_usable_assessment`, `freshness`, and
`notification` attributes. For skipped checks, verify the states above; for
camera errors, check the feed and JPEG format. `last_check` means the check
completed, not that the image was usable. `notification: submitted` means the
action succeeded; verify delivery separately.

## Development

Use Python 3.14.2+ on POSIX. Install development dependencies and run checks:

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check.py
```

[Contributing](CONTRIBUTING.md) · [Apache 2.0 license](LICENSE)
