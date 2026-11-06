"""Exercise real HA lifecycle APIs with synthetic frames and fake provider services."""

import asyncio
import argparse
import importlib
import importlib.metadata
import io
import json
import logging
import platform
import hashlib
import os
import subprocess
import sys
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time

from PIL import Image
import orjson
from packaging.requirements import Requirement
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/p1s_guard"
problems = []


class ProblemCapture(logging.Handler):
    def emit(self, record):
        problems.append(record.name + ": " + self.format(record))


logging.basicConfig(level=logging.ERROR, handlers=[ProblemCapture()])


def environment_summary():
    """Export fixed labels and logger counts rather than raw Core messages."""
    counts = {}
    for problem in problems:
        source = problem.split(":", 1)[0]
        counts[source] = counts.get(source, 0) + 1
    labels = []
    for fragment, label in (
        ("libturbojpeg", "optional_turbojpeg_unavailable"),
        ("ffmpeg", "local_ffmpeg_unavailable"),
    ):
        if any(fragment in problem.lower() for problem in problems):
            labels.append(label)
    requirement = next(
        Requirement(item)
        for item in importlib.metadata.requires("homeassistant")
        if Requirement(item).name == "orjson"
    )
    return {
        "environment_notes": labels,
        "core_error_counts": counts,
        "core_orjson_requirement": str(requirement.specifier),
        "orjson_matches_core_pin": orjson.__version__ in requirement.specifier,
    }


def configuration(count):
    return {
        "ai_task": "ai_task.example_vision",
        "notify_service": "notify.example_phone",
        "printers": [
            {
                "name": f"Printer {number}",
                "entity_prefix": f"printer_{number}",
                "camera": f"camera.printer_{number}",
            }
            for number in range(1, count + 1)
        ],
    }


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), (100, 120, 140)).save(output, format="JPEG")
    return output.getvalue()


async def start(root, settings, missing_media=False):
    media = root / "media"
    if not missing_media:
        media.mkdir(exist_ok=True)
        if not (media / "unrelated.txt").exists():
            (media / "unrelated.txt").write_text(
                "preserve this unrelated synthetic file"
            )
    if not (root / "unrelated.txt").exists():
        (root / "unrelated.txt").write_text("preserve unrelated configuration data")
    component = root / "custom_components/p1s_guard"
    component.parent.mkdir(exist_ok=True)
    if component.exists():
        shutil.rmtree(component)
    shutil.copytree(SOURCE, component, ignore=shutil.ignore_patterns("__pycache__"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    hass = HomeAssistant(str(root))
    loader.async_setup(hass)
    hass.config.skip_pip = True
    config = {
        "homeassistant": {
            "media_dirs": {"local": str(media)},
            "time_zone": "UTC",
            "latitude": 0,
            "longitude": 0,
            "elevation": 0,
            "unit_system": "metric",
        },
        "http": {"server_host": "127.0.0.1", "server_port": port},
    }
    if settings is not None:
        config["p1s_guard"] = settings
    try:
        async with asyncio.timeout(30):
            await bootstrap.async_from_config_dict(config, hass)
            await hass.async_block_till_done()
            await hass.async_start()
            await hass.async_block_till_done()
    except BaseException:
        await hass.async_stop(force=True)
        raise
    return hass, media


def eligible(hass, runtime):
    for printer in runtime.printers:
        for domain, suffix, value in (
            ("sensor", "print_status", "running"),
            ("sensor", "current_stage", "printing"),
            ("binary_sensor", "online", "on"),
            ("binary_sensor", "print_error", "off"),
            ("sensor", "task_name", "synthetic job"),
            ("sensor", "start_time", "synthetic start"),
        ):
            hass.states.async_set(printer.entity(domain, suffix), value)
        hass.states.async_set(printer.camera, "idle")


async def run_case(root, count, boundary, expect_existing_registry=False):
    problem_start = len(problems)
    hass, media = await start(root, configuration(count))
    runtime = hass.data["p1s_guard"]
    calls = []
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()
    worker_entered = threading.Event()
    worker_release = threading.Event()
    original = None
    try:
        assert not any(
            problem.startswith("homeassistant.setup:")
            for problem in problems[problem_start:]
        ), "unexpected Core setup failure"
        assert all(not monitor.allow_pause for monitor in runtime.jobs.values())
        if expect_existing_registry:
            assert (
                er.async_get(hass).async_get_entity_id(
                    "sensor", "example", "unrelated_fixture_entity"
                )
                is not None
            )
        sensors = [
            state
            for state in hass.states.async_all()
            if state.entity_id.endswith("_printer_guard")
        ]
        assert len(sensors) == count, (count, len(sensors))
        # Missing runtime entities must skip before any camera/provider work.
        for name, monitor in runtime.jobs.items():
            await runtime.run(name, monitor)
        assert all(
            item["outcome"] == "skipped" for item in runtime.diagnostics.values()
        )
        eligible(hass, runtime)
        capture_class = importlib.import_module(
            "custom_components.p1s_guard.engine"
        ).Capture

        async def capture():
            return capture_class(jpeg(), time.monotonic())

        for monitor in runtime.jobs.values():
            monitor.capture = capture

        async def provider(call):
            calls.append(len(call.data["attachments"]))
            provider_entered.set()
            if boundary == "inference":
                await provider_release.wait()
            return {
                "data": {
                    "frame_1_judgeable": True,
                    "frame_1_score": 0.1,
                    "frame_1_evidence": "synthetic clear frame",
                }
            }

        hass.services.async_register(
            "ai_task",
            "generate_data",
            provider,
            supports_response=SupportsResponse.ONLY,
        )
        if boundary == "clear":
            await asyncio.gather(
                *(runtime.run(name, monitor) for name, monitor in runtime.jobs.items())
            )
            assert all(
                item["outcome"] == "clear" and item["last_usable_assessment"]
                for item in runtime.diagnostics.values()
            )
            assert calls == [1] * count, calls
        else:
            if boundary in ("prepare", "cleanup"):
                attribute = "prepare" if boundary == "prepare" else "cleanup"
                original = getattr(runtime.staging, attribute)

                def blocked(*args):
                    worker_entered.set()
                    if not worker_release.wait(5):
                        raise TimeoutError("synthetic worker release was not received")
                    return original(*args)

                setattr(runtime.staging, attribute, blocked)
            runtime.started -= 121
            runtime.tick(None)
            if boundary == "inference":
                await asyncio.wait_for(provider_entered.wait(), 3)
            else:
                assert await asyncio.to_thread(worker_entered.wait, 3)
            stopping = asyncio.create_task(hass.async_stop(force=True))
            async with asyncio.timeout(3):
                while not runtime.stopping:
                    await asyncio.sleep(0)
            provider_release.set()
            worker_release.set()
            await asyncio.wait_for(stopping, 10)
            await asyncio.sleep(0)
            assert runtime.stopping and not runtime.tasks and not runtime.pending
            assert all(
                item.get("reason") == "cancelled"
                for item in runtime.diagnostics.values()
            ), runtime.diagnostics
        assert {p.name for p in media.iterdir()} == {"unrelated.txt"}, list(
            media.iterdir()
        )
        if count == 1 and boundary == "clear":
            er.async_get(hass).async_get_or_create(
                "sensor", "example", "unrelated_fixture_entity"
            )
    finally:
        provider_release.set()
        worker_release.set()
        if not runtime.stopping:
            await hass.async_stop(force=True)
    assert not runtime.tasks and not runtime.pending
    return {
        "printers": count,
        "boundary": boundary,
        "sensors": len(sensors),
        "provider_requests": len(calls),
        "media_cleaned": True,
        "unrelated_file_preserved": True,
        "stop_completed": True,
    }


async def failure_case(root, kind):
    settings = configuration(1)
    marker = "synthetic_protected_configuration_value"
    if kind == "invalid_config":
        settings["ai_task"] = marker
    hass, media = await start(root, settings, missing_media=kind == "missing_media")
    try:
        if kind in ("invalid_config", "missing_media"):
            assert "p1s_guard" not in hass.data
            assert marker not in "\n".join(problems)
            outcome = "setup_disabled"
        else:
            runtime = hass.data["p1s_guard"]
            eligible(hass, runtime)
            if kind != "unavailable_camera":
                capture_class = importlib.import_module(
                    "custom_components.p1s_guard.engine"
                ).Capture

                async def capture():
                    return capture_class(jpeg(), time.monotonic())

                for monitor in runtime.jobs.values():
                    monitor.capture = capture
            if kind == "unavailable_provider":
                hass.services.async_remove("ai_task", "generate_data")
            if kind == "read_only_media":
                media.chmod(0o500)
            for name, monitor in runtime.jobs.items():
                await runtime.run(name, monitor)
            values = list(runtime.diagnostics.values())
            assert all(
                item["outcome"] == "failure" and item["notification"] == "not_requested"
                for item in values
            ), values
            expected = {
                "unavailable_camera": "camera_capture",
                "unavailable_provider": "ai_service",
                "read_only_media": "media_prepare",
            }[kind]
            assert all(item.get("failure_phase") == expected for item in values), values
            assert not any(
                token in json.dumps(values)
                for token in ("camera.", "ai_task.", "/tmp/", marker)
            )
            outcome = "fixed_failure_diagnostic"
            if kind == "read_only_media":
                media.chmod(0o700)
            assert {p.name for p in media.iterdir()} == {"unrelated.txt"}
        assert (
            root / "unrelated.txt"
        ).read_text() == "preserve unrelated configuration data"
    finally:
        if media.exists():
            media.chmod(0o700)
        await hass.async_stop(force=True)
    return {"negative_case": kind, "outcome": outcome, "unrelated_data_preserved": True}


async def matrix():
    results = []
    negative = (
        "invalid_config",
        "missing_media",
        "read_only_media",
        "unavailable_camera",
        "unavailable_provider",
    )
    with tempfile.TemporaryDirectory(prefix="printguard-real-ha-") as directory:
        root = Path(directory)
        for number, (count, boundary) in enumerate(
            (
                (1, "clear"),
                (2, "clear"),
                (1, "prepare"),
                (1, "inference"),
                (1, "cleanup"),
            )
        ):
            case = root / str(number)
            case.mkdir()
            results.append(await run_case(case, count, boundary))
        # Reuse a stopped installation directory with Guard omitted from config.
        hass, media = await start(root / "0", None)
        try:
            assert "p1s_guard" not in hass.data
            restored = [
                s
                for s in hass.states.async_all()
                if s.entity_id.endswith("_printer_guard")
            ]
            assert all(
                s.state == "unavailable" and s.attributes.get("restored")
                for s in restored
            ), [(s.state, s.attributes) for s in restored]
            registry = er.async_get(hass)
            entries = [
                entry
                for entry in registry.entities.values()
                if entry.platform == "p1s_guard"
            ]
            assert len(entries) == 1
            # This is the registry API used by HA settings, never a storage-file edit.
            for entry in entries:
                registry.async_remove(entry.entity_id)
            await hass.async_block_till_done()
            assert not [
                s
                for s in hass.states.async_all()
                if s.entity_id.endswith("_printer_guard")
            ]
            assert (
                registry.async_get_entity_id(
                    "sensor", "example", "unrelated_fixture_entity"
                )
                is not None
            )
            assert {p.name for p in media.iterdir()} == {"unrelated.txt"}
        finally:
            await hass.async_stop(force=True)
        results.append(
            {
                "removal_after_stop": True,
                "inactive_registry_entries_removed_via_api": True,
                "guard_sensors_absent": True,
                "unrelated_file_and_registry_entry_preserved": True,
            }
        )
        for number, kind in enumerate(negative, 10):
            case = root / str(number)
            case.mkdir()
            results.append(await failure_case(case, kind))
    return {
        "core": importlib.metadata.version("homeassistant"),
        "python": platform.python_version(),
        "orjson": orjson.__version__,
        "real_ha_apis": True,
        "synthetic_frames": True,
        "fake_provider": True,
        "external_provider_calls": 0,
        "external_notifications": 0,
        "printer_commands": 0,
        **environment_summary(),
        "cases": results,
    }


async def alerts():
    with tempfile.TemporaryDirectory(prefix="printguard-alert-path-") as directory:
        hass, media = await start(Path(directory), configuration(1))
        runtime = hass.data["p1s_guard"]
        eligible(hass, runtime)
        name, monitor = next(iter(runtime.jobs.items()))
        printer = runtime.printers[0]
        frame_class = importlib.import_module(
            "custom_components.p1s_guard.engine"
        ).Capture
        elapsed = [0.0]
        captures = []
        requests = []
        notices = []
        comparison_clear = [False]

        async def capture():
            number = len(captures) + 1
            output = io.BytesIO()
            Image.new("RGB", (32, 32), (number * 11 % 256, 80, 110)).save(
                output, format="JPEG"
            )
            captures.append(number)
            return frame_class(output.getvalue(), elapsed[0])

        async def advance(seconds):
            elapsed[0] += seconds
            await asyncio.sleep(0)

        async def provider(call):
            count = len(call.data["attachments"])
            requests.append(count)
            assert count in (1, 3)
            assert call.data["entity_id"] == "ai_task.example_vision"
            return {
                "data": {
                    f"frame_{index}_{key}": value
                    for index in range(1, count + 1)
                    for key, value in (
                        ("judgeable", True),
                        (
                            "score",
                            0.1
                            if comparison_clear[0] and count == 3 and index == 3
                            else 0.9,
                        ),
                        ("evidence", "synthetic assessment"),
                    )
                }
            }

        async def notify(call):
            assert set(call.data) == {"title", "message"}
            assert "No pause was sent" in call.data["message"]
            notices.append("fake_service_returned")

        monitor.capture = capture
        monitor.sleep = advance
        monitor.clock = lambda: elapsed[0]
        hass.services.async_register(
            "ai_task",
            "generate_data",
            provider,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register("notify", "example_phone", notify)
        results = []
        try:
            for expected in ("submitted", "suppressed_cooldown"):
                await runtime.run(name, monitor)
                diagnostic = runtime.diagnostics[name]
                assert diagnostic["outcome"] == "suspicious", diagnostic
                assert diagnostic["notification"] == expected, diagnostic
                assert diagnostic["assessment"] == "suspicious"
                assert (
                    diagnostic["last_usable_assessment"]
                    and diagnostic["freshness"] == "unknown"
                )
                assert len(notices) == 1
                results.append(
                    {
                        "case": expected,
                        "assessment": diagnostic["assessment"],
                        "notification": diagnostic["notification"],
                        "fake_notify_calls": len(notices),
                    }
                )
            hass.states.async_set(
                printer.entity("sensor", "task_name"), "replacement synthetic job"
            )
            await runtime.run(name, monitor)
            diagnostic = runtime.diagnostics[name]
            assert diagnostic["notification"] == "submitted" and len(notices) == 2, (
                diagnostic
            )
            results.append(
                {
                    "case": "new_job",
                    "notification": "submitted",
                    "fake_notify_calls": len(notices),
                }
            )
            comparison_clear[0] = True
            await runtime.run(name, monitor)
            diagnostic = runtime.diagnostics[name]
            assert (
                diagnostic["outcome"] == "not_confirmed"
                and diagnostic["notification"] == "not_requested"
            ), diagnostic
            assert len(notices) == 2
            results.append(
                {
                    "case": "suspicion_clears",
                    "assessment": "not_confirmed",
                    "notification": "not_requested",
                    "fake_notify_calls": len(notices),
                }
            )
            assert requests == [1, 3] * 4 and len(captures) == 12
            assert not monitor.allow_pause and not runtime.latch.data
            assert not runtime.staging.active
            assert {p.name for p in media.iterdir()} == {"unrelated.txt"}
        finally:
            await hass.async_stop(force=True)
        assert not runtime.tasks and not runtime.pending
    report = {
        "core": importlib.metadata.version("homeassistant"),
        "python": platform.python_version(),
        "orjson": orjson.__version__,
        "synthetic_frames": True,
        "virtual_frame_spacing": True,
        "fake_provider": True,
        "fake_notify": True,
        "external_provider_calls": 0,
        "external_notifications": 0,
        "printer_commands": 0,
        "provider_requests": len(requests),
        "frames": len(captures),
        "cases": results,
        "stop_completed": True,
        "media_cleaned": True,
        **environment_summary(),
    }
    return report


def source_files(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


async def service_contract():
    """Use HA's actual AI Task service schema, entity routing and media resolver."""
    from homeassistant.components.ai_task import (
        AITaskEntity,
        AITaskEntityFeature,
        GenDataTaskResult,
    )

    with tempfile.TemporaryDirectory(
        prefix="printguard-service-contract-"
    ) as directory:
        hass, media = await start(Path(directory), configuration(1))
        runtime = hass.data["p1s_guard"]
        calls = []
        full_features = (
            AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
        )
        features = [full_features]
        response_mode = ["valid"]
        protected_response = "synthetic_protected_response_value"
        failures = []

        class SyntheticAI(AITaskEntity):
            _attr_name = "Example Vision"
            _attr_unique_id = "printguard_synthetic_ai"

            @property
            def supported_features(self):
                return features[0]

            async def _async_generate_data(self, task, chat_log):
                assert task.attachments and len(task.attachments) == 1
                attachment = task.attachments[0]
                assert attachment.mime_type == "image/jpeg"
                assert attachment.path.is_relative_to(media)
                assert attachment.path.read_bytes() == jpeg()
                data = {
                    "frame_1_judgeable": True,
                    "frame_1_score": 0.1,
                    "frame_1_evidence": "synthetic clear frame",
                }
                if response_mode[0] == "malformed":
                    data["frame_1_score"] = protected_response
                else:
                    assert task.structure(data) == data
                calls.append("resolved_synthetic_attachment")
                return GenDataTaskResult(conversation_id="synthetic_session", data=data)

        try:
            await hass.data["ai_task"].async_add_entities([SyntheticAI()])
            await hass.async_block_till_done()
            assert hass.states.get("ai_task.example_vision") is not None
            eligible(hass, runtime)
            name, monitor = next(iter(runtime.jobs.items()))
            frame_class = importlib.import_module(
                "custom_components.p1s_guard.engine"
            ).Capture

            async def capture():
                return frame_class(jpeg(), time.monotonic())

            monitor.capture = capture
            await runtime.run(name, monitor)
            assert runtime.diagnostics[name]["outcome"] == "clear", runtime.diagnostics[
                name
            ]
            assert calls == ["resolved_synthetic_attachment"]
            assert {path.name for path in media.iterdir()} == {"unrelated.txt"}
            adapter = monitor.screen.__self__
            printer = runtime.printers[0]
            for case, entity, support, mode, phase, category in (
                (
                    "missing_entity",
                    "ai_task.missing_synthetic_entity",
                    full_features,
                    "valid",
                    "ai_service",
                    "validation",
                ),
                (
                    "unsupported_attachments",
                    "ai_task.example_vision",
                    AITaskEntityFeature.GENERATE_DATA,
                    "valid",
                    "ai_service",
                    "validation",
                ),
                (
                    "malformed_response",
                    "ai_task.example_vision",
                    full_features,
                    "malformed",
                    "screen_verdict",
                    "invalid_data",
                ),
            ):
                adapter.entity_id = entity
                features[0] = support
                response_mode[0] = mode
                hass.states.async_set(printer.entity("sensor", "task_name"), case)
                await runtime.run(name, monitor)
                diagnostic = runtime.diagnostics[name]
                assert diagnostic["outcome"] == "failure", diagnostic
                assert diagnostic["failure_phase"] == phase, diagnostic
                assert diagnostic["failure_category"] == category, diagnostic
                assert diagnostic["notification"] == "not_requested"
                assert diagnostic["last_usable_assessment"] is None
                assert protected_response not in json.dumps(diagnostic)
                assert not any(
                    token in json.dumps(diagnostic)
                    for token in ("ai_task.", "camera.", "/tmp/")
                )
                assert {path.name for path in media.iterdir()} == {"unrelated.txt"}
                failures.append({"case": case, "phase": phase, "category": category})
            assert protected_response not in "\n".join(problems)
            response_mode[0] = "valid"
            hass.states.async_set(
                printer.entity("sensor", "task_name"), "recovered synthetic job"
            )
            await runtime.run(name, monitor)
            assert runtime.diagnostics[name]["outcome"] == "clear", runtime.diagnostics[
                name
            ]
            assert runtime.diagnostics[name]["last_usable_assessment"]
            assert len(calls) == 3
            assert not monitor.allow_pause and not runtime.latch.data
            assert not runtime.staging.active
            assert {path.name for path in media.iterdir()} == {"unrelated.txt"}
        finally:
            await hass.async_stop(force=True)
    return {
        "actual_ai_service_schema": True,
        "actual_entity_routing": True,
        "actual_local_media_resolution": True,
        "actual_service_failure_cases": failures,
        "recovery_after_failures": True,
        "fake_provider_requests": len(calls),
        "external_provider_calls": 0,
        "external_notifications": 0,
        "printer_commands": 0,
        "media_cleaned": True,
        "stop_completed": True,
    }


def main():
    global SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--previous",
        type=Path,
        help="Optional previous p1s_guard component folder for cold-process update and rollback checks",
    )
    parser.add_argument("--report", type=Path, help="Write a fixed-field JSON report")
    parser.add_argument(
        "--_worker",
        choices=["matrix", "alerts", "service_contract", "install"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--_source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_existing", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if importlib.metadata.version("homeassistant") != "2026.11.0":
        raise SystemExit(
            "Use the separate requirements-ha-test.txt environment (Core 2026.11.0)."
        )
    mismatches = []
    for item in importlib.metadata.requires("homeassistant"):
        requirement = Requirement(item)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(requirement.name)
            continue
        if installed not in requirement.specifier:
            mismatches.append(requirement.name)
    if mismatches or os.environ.get("PYTHONPATH"):
        raise SystemExit(
            "Use unchanged Core dependencies without a PYTHONPATH overlay."
        )
    if arguments._source:
        SOURCE = arguments._source.resolve(strict=True)
    if arguments._worker:
        if arguments._worker == "matrix":
            result = asyncio.run(matrix())
        elif arguments._worker == "alerts":
            result = asyncio.run(alerts())
        elif arguments._worker == "service_contract":
            result = asyncio.run(service_contract())
        else:
            fixture = arguments._fixture.resolve(strict=True)
            assert fixture.name == "installation"
            assert fixture.parent.name.startswith("printguard-ha-check-")
            assert (
                fixture.parent / ".synthetic-fixture"
            ).read_text() == "printguard synthetic data only\n"
            result = asyncio.run(
                run_case(
                    fixture, 1, "clear", expect_existing_registry=arguments._existing
                )
            )
        print(json.dumps(result))
        return
    sources = {SOURCE: source_files(SOURCE)}
    if arguments.previous:
        previous = arguments.previous.resolve(strict=True)
        assert (
            json.loads((previous / "manifest.json").read_text())["domain"]
            == "p1s_guard"
        )
        sources[previous] = source_files(previous)
    results = []
    with tempfile.TemporaryDirectory(prefix="printguard-ha-check-") as directory:
        root = Path(directory)
        (root / ".synthetic-fixture").write_text("printguard synthetic data only\n")
        fixture = root / "installation"
        fixture.mkdir()
        steps = [("matrix", SOURCE), ("alerts", SOURCE), ("service_contract", SOURCE)]
        if arguments.previous:
            steps.extend(
                [
                    ("install_previous", previous),
                    ("update_candidate", SOURCE),
                    ("rollback_previous", previous),
                ]
            )
        for name, source in steps:
            mode = (
                name if name in ("matrix", "alerts", "service_contract") else "install"
            )
            command = [
                sys.executable,
                "-B",
                "-X",
                "faulthandler",
                str(Path(__file__).resolve()),
                "--_worker",
                mode,
                "--_source",
                str(source),
            ]
            if mode == "install":
                command.extend(["--_fixture", str(fixture)])
                if name != "install_previous":
                    command.append("--_existing")
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if process.returncode:
                raise SystemExit(
                    f"Synthetic {name} worker failed (process exit {process.returncode})."
                )
            result = json.loads(process.stdout)
            if mode == "install":
                assert (
                    source_files(fixture / "custom_components/p1s_guard")
                    == sources[source]
                )
                assert (
                    fixture / "unrelated.txt"
                ).read_text() == "preserve unrelated configuration data"
                assert (
                    fixture / "media/unrelated.txt"
                ).read_text() == "preserve this unrelated synthetic file"
            results.append(
                {"step": name, "process_exit": process.returncode, "result": result}
            )
        assert all(
            source_files(source) == expected for source, expected in sources.items()
        )
    report = {
        "core": importlib.metadata.version("homeassistant"),
        "python": platform.python_version(),
        "orjson": orjson.__version__,
        "core_dependency_mismatches": mismatches,
        "synthetic_frames": True,
        "fake_provider": True,
        "fake_notify": True,
        "external_provider_calls": 0,
        "external_notifications": 0,
        "printer_commands": 0,
        "previous_version_checks": "passed"
        if arguments.previous
        else "skipped_no_previous_source",
        "results": results,
    }
    if arguments.report:
        arguments.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
