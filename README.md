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

1. Copy [`custom_components/printer_guard`](custom_components/printer_guard) into your
   Home Assistant configuration directory's `custom_components/` folder.
2. Adapt the [YAML example](examples/configuration.yaml) for one or two printers.
   Set `homeassistant.media_dirs.local` to an existing, writable absolute path
   on a local POSIX filesystem.
   `ai_task` handles the follow-up comparison. Add optional `screen_ai_task` to
   use a different AI Task for the initial single-frame check. If omitted, both
   stages use `ai_task`. Both tasks must support images and structured data.
3. Check the printer entities below, run Home Assistant's configuration check,
   and restart Home Assistant.

### Replacing an earlier installation

Changing the integration domain is a fresh installation in Home Assistant.
Before replacing a running installation, back up its configuration and record
the two diagnostic entity IDs, dashboard references, and any automations that
use them. Replace the component directory and the top-level YAML key together,
then validate the configuration before restarting. Keep a copy of the old
component and YAML so the previous installation can be restored if validation
fails. Do not delete the shared media directory: inspect any old staging
directories and cleanup records with the previous component's recovery
procedure before removing it.

After a successful configuration check and restart, confirm both diagnostics
load under the new integration domain. Home Assistant can retain registry entries
from the old domain; remove only those entries through Entities settings after
checking that no dashboard, automation, script, scene, or helper still uses them.
The new diagnostic unique IDs are different, so existing diagnostic entity IDs
may need to be assigned to the new entries through Entities settings. Check
monitoring outcomes after the startup delay; this does not verify alert delivery.

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
