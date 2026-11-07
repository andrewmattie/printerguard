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
| `printers[].entity_prefix` | Printer entity prefix | Binds the six fixed printer entities listed in the README. |
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

Open the entry's **Configure** action to edit its name, camera, tasks or notification
action. The printer prefix is fixed because it identifies the monitored printer.
Clear the optional screening task field to use the comparison task again. A
configured screen task that later becomes unavailable fails that assessment; it
never silently switches providers.

Saving options unloads and reloads only that UI entry. The old timer and stop
listener are removed, in-flight work is canceled and awaited, and owned temporary
media is cleaned before another runtime starts. Checks restart after the usual
two-minute delay. Notification cooldowns survive UI reloads and unloads within the process; a Home
Assistant restart clears them. Other configured printers continue running.

## Keep managing YAML

The [example](../examples/configuration.yaml) remains supported unchanged. YAML
is validated and started directly; there is no config-entry import, one-time
migration, file rewrite or UI synchronization. Edit YAML, validate Home
Assistant configuration, and restart Home Assistant to apply it. Repeated setup
of unchanged YAML in one process does not create another monitor.

YAML-managed printers do not appear as editable UI config entries. Their
diagnostic sensors remain available. A YAML edit is never overwritten by UI
options, and the UI cannot rewrite `configuration.yaml`.

## Conflicts and switching methods

A printer's prefix and camera must each have one owner. UI setup and edits reject
a prefix or camera reserved by YAML or another UI entry, including disabled and
unloaded entries. Distinct printers may use different sources. If YAML is later
edited to collide with a persisted UI entry, YAML is authoritative on startup;
the conflicting UI entry fails setup with an actionable error. It does not start
a duplicate monitor. Resolve the conflict and reload the UI entry.

Switch explicitly, one printer at a time:

- YAML to UI: note the existing settings, remove that printer from YAML (remove
  the entire `printer_guard` section if none remain), validate and restart, then
  add a UI entry with the same prefix, camera and settings.
- UI to YAML: note the UI settings, delete that UI entry through Devices &
  services, add the same settings to YAML, validate and restart.

Diagnostic unique IDs are based on the printer prefix and remain stable across
reloads and source changes. Existing registered sensor IDs and user
customizations are retained where Home Assistant retains their registry entry.
Deleting a UI entry removes its registry entities as usual; if it is later
recreated, Home Assistant may assign a different display entity ID. Reusing the
same prefix preserves the integration's unique ID, not a deleted registry record.

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
