# Printer Guard

Printer Guard is an experimental, alert-only Home Assistant integration for 3D
printers. It uses an AI Task to check a camera frame for print failures. If the
first frame looks suspicious, it takes two more and alerts you if the AI flags
all three in the comparison.

Checks start after a two-minute startup delay and run on a one-minute scheduler.
The extra frames are taken about 30 seconds apart. After a notification action
returns successfully, repeats for that printer's current job are suppressed for
30 minutes. A new job does not inherit the previous job's cooldown; restarting HA
resets the in-memory cooldown.

Developed against Home Assistant Core 2026.10.4. The A1 Mini has produced a usable
live assessment; P1S and notification-delivery verification remain incomplete.
Test only on supervised prints. False alarms and missed failures are possible;
keep checking the printer yourself. Different image bytes do not prove a live view.

## Setup

You'll need a JPEG camera for each printer, an `ai_task` entity that accepts
images and returns structured data, and a `notify.<service>` action that accepts
`title` and `message`. Checks can send images and printer telemetry to your AI
provider and incur charges. Read the [privacy notes](PRIVACY.md) before enabling
monitoring.

1. Copy [`custom_components/p1s_guard`](custom_components/p1s_guard) into your
   Home Assistant configuration directory's `custom_components/` folder.
2. Adapt the [YAML example](examples/configuration.yaml) for one or two printers.
   Set `homeassistant.media_dirs.local` to an existing, writable absolute path
   on a local POSIX filesystem.
3. Check the printer entities below, run Home Assistant's configuration check,
   and restart Home Assistant.

For `entity_prefix: printer_one`, the camera must be available and these entities
must have the following states.

| Entity | State |
| --- | --- |
| `sensor.printer_one_print_status` | `running` |
| `sensor.printer_one_current_stage` | `printing` |
| `binary_sensor.printer_one_online` | `on` |
| `binary_sensor.printer_one_print_error` | `off` |
| `sensor.printer_one_task_name` | Current job name |
| `sensor.printer_one_start_time` | Current job start time |

See [diagnostics](DIAGNOSTICS.md) for skipped checks and camera errors.

## Development

Use Python 3.14.2 or newer on a POSIX host and install the development dependencies.

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check.py
```

[Contributing](CONTRIBUTING.md) · [Apache 2.0 license](LICENSE)
