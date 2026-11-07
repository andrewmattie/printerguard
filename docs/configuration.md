# Configuration and ownership

Printer Guard supports YAML and Home Assistant UI configuration as continuing
choices. At most two printers can be configured in total. A setup uses the same
validated model and monitor-only runtime through either path. This does not
claim Home Assistant Core acceptance or an Integration Quality Scale tier.

## Configure through the UI

After installing and restarting Home Assistant, open **Settings → Devices &
services → Add integration → Printer Guard**. Add one entry for each printer.
Configure the required entities and local media directory before opening the flow.

| YAML field | UI field | Purpose |
| --- | --- | --- |
| `printers[].name` | Printer name | Diagnostic sensor and alert title. |
| `printers[].print_status` | Print status | Sensor reporting `running`. |
| `printers[].current_stage` | Current stage | Sensor reporting `printing`. |
| `printers[].online` | Online | Binary sensor reporting `on`. |
| `printers[].print_error` | Print error | Binary sensor reporting `off`. |
| `printers[].task_name` | Task name | Sensor identifying the current job. |
| `printers[].start_time` | Start time | Sensor identifying the current job's start. |
| `printers[].current_layer` | Current layer | Optional sensor telemetry. |
| `printers[].total_layer_count` | Total layers | Optional sensor telemetry. |
| `printers[].print_progress` | Print progress | Optional sensor telemetry. |
| `printers[].nozzle_temperature` | Nozzle temperature | Optional sensor telemetry. |
| `printers[].bed_temperature` | Bed temperature | Optional sensor telemetry. |
| `printers[].camera` | Camera | Explicit JPEG camera entity. |
| `ai_task` | Comparison AI Task | Image-capable structured-data task for comparison. |
| `screen_ai_task` | Screening AI Task | Optional initial-frame task; omission uses `ai_task`. |
| `notify_service` | Notification action | Existing `notify` action accepting title and message. |

Each UI entry has independent task and notification routing. YAML continues to
share those settings among its one or two printers, exactly as before. No new
runtime tuning options are introduced by this feature.

Validation checks syntax, existing bindings, available camera and AI Task
entities, AI generation/attachment capabilities, registered notification action,
and local media configuration. It does not test image support at the provider,
capture frames, invoke paid AI, send alerts, or issue printer commands. Actual
assessment and delivery still require supervised validation. The provider receives
frames and telemetry during normal assessments and may charge; check its terms.

Open the entry's **Configure** action to edit its name, printer entities, camera,
tasks or notification action. Select each entity directly; no shared naming
prefix is required. The entry keeps its diagnostic identity when bindings change.
Clear an optional telemetry field to stop reading that sensor. Clear the optional
screening task field to use the comparison task again. A configured screen task that later becomes unavailable fails that assessment; it
never silently switches providers.

Saving options unloads and reloads only that UI entry. The old timer and stop
listener are removed, in-flight work is canceled and awaited, and owned temporary
media is cleaned before another runtime starts. Checks restart after the usual
two-minute delay. Notification cooldowns survive UI reloads and unloads within the process; a Home
Assistant restart clears them. Other configured printers continue running.

## Keep managing YAML

The [example](../examples/configuration.yaml) shows explicit bindings. YAML
is validated and started directly; there is no config-entry import, file rewrite
or UI synchronization. Edit YAML, validate Home Assistant configuration, and
restart Home Assistant to apply it. Repeated setup
of unchanged YAML in one process does not create another monitor.

YAML-managed printers do not appear as editable UI config entries. Their
diagnostic sensors remain available. A YAML edit is never overwritten by UI
options, and the UI cannot rewrite `configuration.yaml`.

## Conflicts and switching methods

A printer's identity and every selected printer entity, including optional
telemetry, must each have one owner. Each role needs a distinct entity. UI setup
and edits reject bindings or identities reserved by YAML or another UI entry,
including disabled and unloaded entries. Distinct printers may use different
sources. If YAML is later edited to collide with a persisted UI entry, YAML is authoritative on startup;
the conflicting UI entry fails setup with an actionable error. It does not start
a duplicate monitor. Resolve the conflict and reload the UI entry.

Switch explicitly, one printer at a time:

- YAML to UI: note the existing settings, remove that printer from YAML (remove
  the entire `printer_guard` section if none remain), validate and restart, then
  add a UI entry with the same explicit entities, camera and settings.
- UI to YAML: note the UI settings, delete that UI entry through Devices &
  services, add the same settings to YAML, validate and restart.

New printer identities are derived from the selected print-status entity. For
`sensor.<prefix>_print_status`, the legacy `<prefix>` identity is retained;
otherwise the complete entity ID is used. UI entries keep that saved identity
after edits. Reusing an identity with disjoint entities is also a conflict.
Existing registered sensor IDs and user customizations are retained where Home
Assistant retains their registry entry. Deleting a UI entry removes its registry
entities as usual; recreating it may assign a different display entity ID.
Switching sources preserves logical identity when the same print-status binding
is used and the UI entry has not previously been rebound to another status entity.

## Existing prefix-based configuration

Legacy YAML with only `name`, `entity_prefix` and `camera` remains supported.
It expands to the previous six required sensors and five telemetry sensors in
memory, without rewriting YAML. To convert it, replace `entity_prefix` with the
explicit bindings shown in the example. Preserve the original print-status
entity to keep its diagnostic and durable latch identity. Do not mix a prefix
with direct entity fields in the same printer mapping.

Existing version-1 UI entries are upgraded to explicit bindings automatically,
including separately saved options. Their unique IDs, diagnostic identities and
latch keys remain unchanged. Migration does not require entities to be available
and never captures images or calls services. Previously inferred optional sensors
that are missing do not block unrelated edits while their bindings stay unchanged;
clear those fields if you no longer need them. Newly selected telemetry entities
must exist. New UI forms do not ask for a prefix. Older integration versions
cannot read version-2 UI entries or explicit YAML. Keep a Home Assistant backup
from before upgrading if you need to restore the old version and configuration.

Direct selection does not translate printer states: the required states listed
above still apply. Missing or unavailable required runtime states skip checks;
omitted or unavailable optional telemetry is sent as missing data.

## Unload, remove and restart

UI entry reload/unload uses Home Assistant's integration lifecycle and stops only
that entry. Deleting a UI entry removes its sensors and releases its bindings.
If Home Assistant reports a failed unload, the previous runtime keeps its actual
bindings reserved alongside saved options. Home Assistant may require a restart
before it can load the newly saved settings.

YAML removal requires a restart; inactive legacy diagnostic registry entries can
then be removed in Home Assistant's entity settings if desired. Neither path
removes unrelated media or configuration. Interrupted flows create no entries,
and canceled edits do not change saved settings or running monitors.

Config entries and options persist through normal Home Assistant storage and
restart. Media staging and its global quotas are shared across both sources;
startup recovery verifies owned files before monitoring. Physical printer controls
remain disabled. No configuration path makes an AI accuracy or camera freshness
guarantee.
