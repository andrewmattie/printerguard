# Printer Guard

Printer Guard is an experimental, alert-only Home Assistant integration for 3D
printers. It uses an AI Task to check a camera frame for print failures. If the
first frame looks suspicious, it takes two more and alerts you if the AI flags
all three in the comparison.

Live monitoring is still unverified because of an open camera validation issue.
Test it only on supervised prints.

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

Run the tests with Python 3.10 or newer on a POSIX host.

```sh
python3 -m unittest discover -s tests -v
```

[Contributing](CONTRIBUTING.md) · [Apache 2.0 license](LICENSE)
