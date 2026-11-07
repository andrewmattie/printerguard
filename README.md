# Printer Guard

Alert-only Home Assistant integration for 3D printers. AI screens
one camera frame; if suspicious, it compares two more about 30 seconds apart. It
alerts only if all three frames are flagged.

Checks start after a two-minute delay and run every minute. After a successful
notify action, repeats for that printer and job are suppressed for 30 minutes.
New jobs don't inherit the cooldown; Home Assistant restarts clear it.

Developed against Home Assistant Core 2026.10.4. Validate live camera assessments
and notification delivery in your setup.
Supervise prints and keep checking the printer: false alarms and missed failures
are possible. Different image bytes do not prove a live view.

## Setup

Configure a JPEG camera per printer, an image-capable `ai_task` returning
structured data, and a `notify.<service>` accepting `title` and `message`. The
provider receives frames and telemetry and may charge. Check retention, training,
access, and billing terms. Frame narrowly; protect real images, credentials, and
logs.

1. Copy [`custom_components/printer_guard`](custom_components/printer_guard) into
   Home Assistant's `custom_components/` folder.
2. Merge the [YAML example](examples/configuration.yaml) for one or two printers.
   Set `homeassistant.media_dirs.local` to an existing, writable absolute path
   on a local POSIX filesystem. `ai_task` handles comparison; optional
   `screen_ai_task` handles the initial frame and defaults to `ai_task`. Both
   need image and structured-data support.
3. Confirm the entities below, validate the configuration, and restart Home
   Assistant.

For `entity_prefix: printer_one`, the camera must be available and these entities
must have these states:

| Entity | State |
| --- | --- |
| `sensor.printer_one_print_status` | `running` |
| `sensor.printer_one_current_stage` | `printing` |
| `binary_sensor.printer_one_online` | `on` |
| `binary_sensor.printer_one_print_error` | `off` |
| `sensor.printer_one_task_name` | Current job name |
| `sensor.printer_one_start_time` | Current job start time |

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
