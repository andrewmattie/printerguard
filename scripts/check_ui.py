"""Disposable, real-Core config-flow and config-entry lifecycle checks.

Invoked by check_ha.py in its pinned environment. All entities, services and
storage are synthetic; no installed Home Assistant instance is contacted.
"""

import asyncio
import copy
import importlib
from unittest.mock import AsyncMock, patch

from homeassistant.components.ai_task import AITaskEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceRegistry, SupportsResponse
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import entity_registry as er

DOMAIN = "printer_guard"


def form(number=1):
    return {
        "name": f"Synthetic Printer {number}",
        "entity_prefix": f"ui_printer_{number}",
        "camera": f"camera.ui_printer_{number}",
        "ai_task": "ai_task.synthetic_comparison",
        "screen_ai_task": "ai_task.synthetic_screen",
        "notify_service": "notify.synthetic_phone",
    }


def install_entities(hass, values):
    """Provide all safety bindings but leave the printer idle during setup."""
    prefix = values["entity_prefix"]
    for domain, suffix, value in (
        ("sensor", "print_status", "idle"),
        ("sensor", "current_stage", "idle"),
        ("binary_sensor", "online", "on"),
        ("binary_sensor", "print_error", "off"),
        ("sensor", "task_name", "synthetic job"),
        ("sensor", "start_time", "synthetic start"),
    ):
        hass.states.async_set(f"{domain}.{prefix}_{suffix}", value)
    hass.states.async_set(values["camera"], "idle")
    features = int(
        AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )
    for key in ("ai_task", "screen_ai_task"):
        if values.get(key):
            hass.states.async_set(values[key], "idle", {"supported_features": features})


def install_services(hass, calls):
    async def provider(call):
        calls.append("provider")
        raise AssertionError("configuration must not call a provider")

    async def notify(call):
        calls.append("notify")
        raise AssertionError("configuration must not send notifications")

    hass.services.async_register(
        "ai_task", "generate_data", provider, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("notify", "synthetic_phone", notify)


async def submit(hass, values):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form" and result["step_id"] == "user", result
    return await hass.config_entries.flow.async_configure(result["flow_id"], values)


async def expect_error(hass, values, error):
    initial = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    try:
        try:
            result = await hass.config_entries.flow.async_configure(initial["flow_id"], values)
        except InvalidData:
            # Required fields and selector domains are rejected by Core's schema
            # before the integration's step is invoked.
            assert error == "invalid_config"
        else:
            assert result["type"] == "form" and error in result["errors"].values(), result
    finally:
        hass.config_entries.flow.async_abort(initial["flow_id"])


def sensor_id(hass, prefix):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"printer_guard_{prefix}_diagnostic"
    )


async def run(root, start, configuration):
    """Exercise flows, storage-backed restart and mixed YAML/UI ownership."""
    calls = []
    hass, media = await start(root, None)
    integration = importlib.import_module(f"custom_components.{DOMAIN}")
    camera = AsyncMock(side_effect=AssertionError("configuration captured an image"))
    checked = []
    original_call = ServiceRegistry.async_call

    async def guarded_call(registry, domain, service, *args, **kwargs):
        if domain in ("ai_task", "notify"):
            calls.append("provider" if domain == "ai_task" else "notify")
            raise AssertionError("configuration must not invoke AI or notification services")
        return await original_call(registry, domain, service, *args, **kwargs)

    with (
        patch.object(integration, "camera_capture", camera),
        patch.object(ServiceRegistry, "async_call", guarded_call),
    ):
        try:
            install_services(hass, calls)
            for number in (1, 2, 3):
                install_entities(hass, form(number))
            # Cancellation must not create any persisted entry.
            cancelled = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            hass.config_entries.flow.async_abort(cancelled["flow_id"])
            assert not hass.config_entries.async_entries(DOMAIN)
            checked.append("cancel_create")

            for key in ("name", "entity_prefix", "camera", "ai_task", "notify_service"):
                missing = form()
                missing.pop(key)
                await expect_error(hass, missing, "invalid_config")
            for key, value in (
                ("name", " "), ("entity_prefix", "Not a prefix"),
                ("camera", "sensor.wrong_domain"), ("ai_task", "camera.wrong_domain"),
                ("notify_service", "light.wrong_domain"),
                ("screen_ai_task", "sensor.wrong_domain"),
            ):
                await expect_error(hass, {**form(), key: value}, "invalid_config")
            await expect_error(hass, {**form(), "camera": "camera.missing"}, "missing_entity")
            await expect_error(hass, {**form(), "ai_task": "ai_task.missing"}, "missing_entity")
            await expect_error(hass, {**form(), "entity_prefix": "missing"}, "missing_printer_entities")
            await expect_error(hass, {**form(), "notify_service": "notify.missing"}, "missing_notify_service")
            for entity in (form()["camera"], form()["ai_task"], form()["screen_ai_task"]):
                hass.states.async_set(entity, "unavailable")
                await expect_error(hass, form(), "unavailable_entity")
                install_entities(hass, form())
            hass.states.async_set(form()["ai_task"], "idle", {"supported_features": 0})
            await expect_error(hass, form(), "unsupported_ai_task")
            install_entities(hass, form())
            media_dirs = hass.config.media_dirs
            hass.config.media_dirs = {}
            try:
                await expect_error(hass, form(), "missing_media")
            finally:
                hass.config.media_dirs = media_dirs
            assert not hass.config_entries.async_entries(DOMAIN)
            checked.append("invalid_and_missing_inputs")

            first_result = await submit(hass, form())
            assert first_result["type"] == "create_entry", first_result
            first = first_result["result"]
            await hass.async_block_till_done()
            assert first.state is ConfigEntryState.LOADED
            assert first.unique_id == form()["entity_prefix"]
            assert first.data["printers"] == [{
                key: form()[key] for key in ("name", "entity_prefix", "camera")
            }]
            assert DOMAIN not in hass.data, "UI must not occupy the YAML runtime slot"
            original_sensor = sensor_id(hass, first.unique_id)
            assert original_sensor and hass.states.get(original_sensor)
            assert er.async_get(hass).async_get(original_sensor).config_entry_id == first.entry_id
            await expect_error(hass, {**form(2), "entity_prefix": form()["entity_prefix"]}, "printer_conflict")
            await expect_error(hass, {**form(2), "camera": form()["camera"]}, "printer_conflict")
            second_result = await submit(hass, form(2))
            assert second_result["type"] == "create_entry", second_result
            second = second_result["result"]
            await hass.async_block_till_done()
            assert second.state is ConfigEntryState.LOADED
            assert second.runtime_data is not first.runtime_data
            assert second.runtime_data.staging is first.runtime_data.staging
            assert second.runtime_data.latch is first.runtime_data.latch
            await expect_error(hass, form(3), "printer_limit")
            checked.append("independent_entries_duplicate_rejection_and_printer_limit")

            # Canceling edits preserves both saved settings and the active runtime.
            old_runtime = first.runtime_data
            original_data = copy.deepcopy(dict(first.data))
            options = await hass.config_entries.options.async_init(first.entry_id)
            assert options["type"] == "form" and options["step_id"] == "init"
            hass.config_entries.options.async_abort(options["flow_id"])
            assert dict(first.data) == original_data and first.runtime_data is old_runtime
            options = await hass.config_entries.options.async_init(first.entry_id)
            edited = {**form(), "name": "Edited synthetic printer"}
            edited.pop("entity_prefix")
            edited.pop("screen_ai_task")
            conflict = await hass.config_entries.options.async_configure(
                options["flow_id"], {**edited, "camera": form(2)["camera"]}
            )
            assert conflict["type"] == "form" and "printer_conflict" in conflict["errors"].values()
            assert first.runtime_data is old_runtime and dict(first.data) == original_data
            result = await hass.config_entries.options.async_configure(options["flow_id"], edited)
            assert result["type"] == "create_entry", result
            await hass.async_block_till_done()
            assert first.state is ConfigEntryState.LOADED and first.runtime_data is not old_runtime
            assert old_runtime.stopping and not old_runtime.tasks and not old_runtime.pending
            assert not old_runtime.observers and old_runtime.unsub is None
            assert first.options["printers"][0]["name"] == edited["name"]
            assert "screen_ai_task" not in first.options
            assert first.unique_id == form()["entity_prefix"]
            assert sensor_id(hass, first.unique_id) == original_sensor
            assert second.runtime_data.stopping is False
            checked.append("edit_cancel_validation_reload_and_stable_sensor")

            # Unload while a synthetic capture is in flight must drain its task.
            runtime = first.runtime_data
            runtime.last_notice[first.unique_id] = ("synthetic job", 1.0)
            entered = asyncio.Event()
            async def blocked_capture():
                entered.set()
                await asyncio.Event().wait()
            monitor = next(iter(runtime.jobs.values()))
            monitor.capture = blocked_capture
            hass.states.async_set(f"sensor.{first.unique_id}_print_status", "running")
            hass.states.async_set(f"sensor.{first.unique_id}_current_stage", "printing")
            runtime.started -= 121
            runtime.tick(None)
            await asyncio.wait_for(entered.wait(), 3)
            assert await hass.config_entries.async_unload(first.entry_id)
            await hass.async_block_till_done()
            assert runtime.stopping and not runtime.tasks and not runtime.pending
            assert not runtime.observers and runtime.unsub is None
            assert not runtime.staging.active
            assert first.state is ConfigEntryState.NOT_LOADED
            assert second.state is ConfigEntryState.LOADED and not second.runtime_data.stopping
            await expect_error(hass, {**form(3), "camera": form()["camera"]}, "printer_conflict")
            assert await hass.config_entries.async_setup(first.entry_id)
            assert await hass.config_entries.async_reload(first.entry_id)
            await hass.async_block_till_done()
            assert first.state is ConfigEntryState.LOADED
            assert sensor_id(hass, first.unique_id) == original_sensor
            assert first.runtime_data.last_notice[first.unique_id] == ("synthetic job", 1.0)
            checked.append("unload_drains_inflight_work_reload_preserves_cooldown")
            ids = first.entry_id, second.entry_id
            assert calls == [] and camera.await_count == 0
        finally:
            await hass.async_stop(force=True)

        # Use HA's actual config-entry store, never edit .storage files.
        hass, media = await start(root, None)
        try:
            install_services(hass, calls)
            first, second = (hass.config_entries.async_get_entry(item) for item in ids)
            assert first and second and first.state is ConfigEntryState.LOADED
            assert second.state is ConfigEntryState.LOADED
            assert first.options["printers"][0]["name"] == "Edited synthetic printer"
            assert "screen_ai_task" not in first.options
            assert sensor_id(hass, first.unique_id) == original_sensor
            checked.append("restart_persists_entries_and_options")
            removed_sensor = sensor_id(hass, second.unique_id)
            second.runtime_data.last_notice[second.unique_id] = ("synthetic job", 1.0)
            assert second.entry_id in hass.data["printer_guard_resources"]["notices"]
            assert await hass.config_entries.async_remove(second.entry_id) == {"require_restart": False}
            await hass.async_block_till_done()
            assert hass.config_entries.async_get_entry(second.entry_id) is None
            assert second.entry_id not in hass.data["printer_guard_resources"]["notices"]
            assert er.async_get(hass).async_get(removed_sensor) is None
            assert hass.states.get(removed_sensor) is None
            assert first.state is ConfigEntryState.LOADED
            checked.append("remove_cleans_entry_and_registry_only")
        finally:
            await hass.async_stop(force=True)

        yaml = configuration(1)
        hass, media = await start(root, yaml)
        try:
            install_services(hass, calls)
            first = hass.config_entries.async_get_entry(ids[0])
            assert first.state is ConfigEntryState.LOADED and DOMAIN in hass.data
            assert hass.config_entries.async_get_entry(ids[1]) is None
            assert hass.data[DOMAIN].staging is first.runtime_data.staging
            assert hass.data[DOMAIN].latch is first.runtime_data.latch
            install_entities(hass, form(3))
            await expect_error(hass, {**form(3), "entity_prefix": "printer_1"}, "printer_conflict")
            await expect_error(hass, {**form(3), "camera": "camera.printer_1"}, "printer_conflict")
            assert await hass.config_entries.async_reload(first.entry_id)
            assert not hass.data[DOMAIN].stopping
            checked.append("yaml_ui_coexist_and_yaml_duplicates_rejected")
        finally:
            await hass.async_stop(force=True)

        # YAML becomes authoritative if edited to collide with a persisted UI entry.
        yaml["printers"][0]["camera"] = form()["camera"]
        hass, media = await start(root, yaml)
        try:
            first = hass.config_entries.async_get_entry(ids[0])
            assert DOMAIN in hass.data
            assert first.state is ConfigEntryState.SETUP_ERROR, first.state
            assert not hass.data[DOMAIN].stopping
            assert calls == [] and camera.await_count == 0
            assert {item.name for item in media.iterdir()} == {"unrelated.txt"}
            checked.append("yaml_wins_restart_collision")
        finally:
            await hass.async_stop(force=True)
        # A failed options reload leaves the old monitor active, while the
        # saved camera change is pending. Both bindings must stay reserved.
        # Core marks FAILED_UNLOAD nonrecoverable, so recovery requires restart.
        for failure in ("false", "exception"):
            failed_root = root / f"failed-unload-{failure}"
            failed_root.mkdir()
            hass, media = await start(failed_root, None)
            try:
                install_services(hass, calls)
                for number in (1, 2, 3):
                    install_entities(hass, form(number))
                created = await submit(hass, form())
                assert created["type"] == "create_entry", created
                entry = created["result"]
                await hass.async_block_till_done()
                original_runtime = entry.runtime_data
                original_camera = form()["camera"]
                replacement_camera = "camera.synthetic_replacement"
                hass.states.async_set(replacement_camera, "idle")
                options = await hass.config_entries.options.async_init(entry.entry_id)
                edited = {**form(), "camera": replacement_camera}
                edited.pop("entity_prefix")
                unload = AsyncMock(return_value=False)
                if failure == "exception":
                    unload.side_effect = RuntimeError("synthetic platform unload failure")
                with patch.object(hass.config_entries, "async_unload_platforms", unload):
                    saved = await hass.config_entries.options.async_configure(
                        options["flow_id"], edited
                    )
                    assert saved["type"] == "create_entry", saved
                    await hass.async_block_till_done()
                unload.assert_awaited_once()
                assert entry.state is ConfigEntryState.FAILED_UNLOAD
                assert entry.runtime_data is original_runtime
                assert not original_runtime.stopping and original_runtime.unsub is not None
                assert original_runtime.printers[0].camera == original_camera
                assert entry.options["printers"][0]["camera"] == replacement_camera
                for claimed_camera in (original_camera, replacement_camera):
                    await expect_error(
                        hass, {**form(2), "camera": claimed_camera}, "printer_conflict"
                    )
                # The retained runtime is another binding reservation, not a
                # second printer: a genuinely independent second entry still fits.
                independent = await submit(hass, form(2))
                assert independent["type"] == "create_entry", independent
                await hass.async_block_till_done()
                assert await hass.config_entries.async_remove(
                    independent["result"].entry_id
                ) == {"require_restart": False}
                failed_id = entry.entry_id
            finally:
                await hass.async_stop(force=True)
            hass, media = await start(failed_root, None)
            try:
                install_services(hass, calls)
                install_entities(hass, form(2))
                recovered = hass.config_entries.async_get_entry(failed_id)
                assert recovered.state is ConfigEntryState.LOADED
                assert recovered.runtime_data.printers[0].camera == replacement_camera
                assert await hass.config_entries.async_reload(recovered.entry_id)
                hass.states.async_set(original_camera, "idle")
                reclaimed = await submit(hass, {**form(2), "camera": original_camera})
                assert reclaimed["type"] == "create_entry", reclaimed
                await hass.async_block_till_done()
                assert reclaimed["result"].state is ConfigEntryState.LOADED
                assert calls == [] and camera.await_count == 0
                checked.append(f"failed_unload_{failure}_reserves_bindings_until_restart")
            finally:
                await hass.async_stop(force=True)

        # Switching sources keeps the diagnostic's logical identity. Removing
        # a UI entry deliberately removes its registry row before YAML recreates it.
        switching = root / "switching"
        switching.mkdir()
        yaml = configuration(1)
        prefix = yaml["printers"][0]["entity_prefix"]
        unique_id = f"printer_guard_{prefix}_diagnostic"
        hass, media = await start(switching, yaml)
        try:
            yaml_sensor = sensor_id(hass, prefix)
            assert yaml_sensor and hass.states.get(yaml_sensor)
            assert er.async_get(hass).async_get(yaml_sensor).unique_id == unique_id
        finally:
            await hass.async_stop(force=True)
        hass, media = await start(switching, None)
        try:
            values = {**yaml["printers"][0], "ai_task": "ai_task.synthetic_comparison",
                      "notify_service": "notify.synthetic_phone"}
            install_entities(hass, values)
            install_services(hass, calls)
            result = await submit(hass, values)
            assert result["type"] == "create_entry", result
            entry = result["result"]
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            assert sensor_id(hass, prefix) == yaml_sensor
            row = er.async_get(hass).async_get(yaml_sensor)
            assert row.unique_id == unique_id and row.config_entry_id == entry.entry_id
            assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
            await hass.async_block_till_done()
            assert er.async_get(hass).async_get(yaml_sensor) is None
            assert hass.states.get(yaml_sensor) is None
        finally:
            await hass.async_stop(force=True)
        hass, media = await start(switching, yaml)
        try:
            assert not hass.config_entries.async_entries(DOMAIN)
            recreated = sensor_id(hass, prefix)
            assert recreated == yaml_sensor and hass.states.get(recreated)
            row = er.async_get(hass).async_get(recreated)
            assert row.unique_id == unique_id and row.config_entry_id is None
            assert calls == [] and camera.await_count == 0
            checked.append("yaml_ui_yaml_switch_preserves_logical_identity")
        finally:
            await hass.async_stop(force=True)
    return {
        "cases": checked, "actual_ha_flow_manager": True,
        "actual_config_entry_storage": True, "synthetic_entities_only": True,
        "configuration_camera_requests": camera.await_count,
        "configuration_provider_requests": calls.count("provider"),
        "configuration_notifications": calls.count("notify"),
        "external_provider_calls": 0, "external_notifications": 0, "printer_commands": 0,
    }
