"""Overlap actual Runtime adapters using synthetic frames and gated fake services."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components'))
from printer_guard import Runtime
from printer_guard.engine import Capture
from guard_fixtures import SETTINGS, synthetic_jpeg


def eligible_states():
    states = {}
    changed = datetime.now(timezone.utc)
    for number, printer in enumerate(SETTINGS.printers, 1):
        for domain, suffix, value in (
            ('sensor', 'print_status', 'running'), ('sensor', 'current_stage', 'printing'),
            ('binary_sensor', 'online', 'on'), ('binary_sensor', 'print_error', 'off'),
            ('sensor', 'task_name', f'synthetic job {number}'),
            ('sensor', 'start_time', f'synthetic start {number}'),
            ('sensor', 'current_layer', str(number * 11)),
        ):
            states[printer.entity(domain, suffix)] = SimpleNamespace(state=value, last_changed=changed)
        states[printer.camera] = SimpleNamespace(state='idle', last_changed=changed)
    return states


class ConcurrentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_and_comparison_requests_overlap_without_crossing_printers(self):
        await self.exercise(fail_first=False)

    async def test_provider_failure_does_not_block_other_printer_or_leave_media(self):
        await self.exercise(fail_first=True)

    async def exercise(self, fail_first):
        entered = {1: asyncio.Event(), 3: asyncio.Event()}
        release = {1: asyncio.Event(), 3: asyncio.Event()}
        waiting = {1: set(), 3: set()}
        requests = []
        notices = []
        ticks = {printer.prefix: 0.0 for printer in SETTINGS.printers}
        captures = {printer.prefix: 0 for printer in SETTINGS.printers}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            class Services:
                async def async_call(inner, domain, service, data, **kwargs):
                    if (domain, service) == ('notify', 'example_phone'):
                        notices.append(data['title'])
                        return None
                    if (domain, service) != ('ai_task', 'generate_data'):
                        raise AssertionError('unexpected service or printer control')
                    count = len(data['attachments'])
                    telemetry = json.loads(data['instructions'].split('Telemetry is context only: ', 1)[1])
                    number = {'11': 1, '22': 2}[telemetry['layer']]
                    base = 0 if number == 1 else 100
                    expected = [base + 1] if count == 1 else [base + n for n in (1, 2, 3)]
                    paths = [root / item['media_content_id'].removeprefix('media-source://media_source/local/') for item in data['attachments']]
                    self.assertEqual([path.read_bytes() for path in paths], [synthetic_jpeg(n) for n in expected])
                    self.assertEqual(data['entity_id'], SETTINGS.ai_task)
                    self.assertTrue(all(path.parent in runtime.staging.active for path in paths))
                    requests.append((count, number, tuple(paths)))
                    waiting[count].add(number)
                    if len(waiting[count]) == 2:
                        entered[count].set()
                    await release[count].wait()
                    if fail_first and number == 1:
                        raise TimeoutError('synthetic-private-provider')
                    score = 0.1 if fail_first else 0.9
                    return {'data': {f'frame_{n}_{key}': value
                        for n in range(1, count + 1)
                        for key, value in (('judgeable', True), ('score', score), ('evidence', 'synthetic assessment'))}}

            async def forbidden(*args):
                raise AssertionError('pause reservation must not be used')

            hass = SimpleNamespace(states=eligible_states(), services=Services())
            runtime = Runtime(hass, root, SimpleNamespace(reserve=forbidden), SETTINGS)
            runtime.staging.startup()

            async def capture(_hass, printer):
                captures[printer.prefix] += 1
                index = captures[printer.prefix]
                base = 0 if printer == SETTINGS.printers[0] else 100
                return Capture(synthetic_jpeg(base + index), ticks[printer.prefix])

            for name, monitor in runtime.jobs.items():
                monitor.clock = lambda key=name: ticks[key]
                async def advance(seconds, key=name):
                    ticks[key] += seconds
                monitor.sleep = advance
                self.assertFalse(monitor.allow_pause)
            tasks = []
            with patch('printer_guard.camera_capture', capture):
                try:
                    tasks = [asyncio.create_task(runtime.run(name, monitor)) for name, monitor in runtime.jobs.items()]
                    await asyncio.wait_for(entered[1].wait(), 3)
                    self.assertEqual(len(runtime.staging.active), 2)
                    self.assertEqual(len({paths[0].parent for _, _, paths in requests}), 2)
                    release[1].set()
                    if not fail_first:
                        await asyncio.wait_for(entered[3].wait(), 3)
                        self.assertEqual(len(runtime.staging.active), 2)
                        release[3].set()
                    if fail_first:
                        with self.assertLogs('printer_guard', level='WARNING'):
                            await asyncio.wait_for(asyncio.gather(*tasks), 3)
                    else:
                        await asyncio.wait_for(asyncio.gather(*tasks), 3)
                finally:
                    for task in tasks:
                        if not task.done(): task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            first, second = SETTINGS.printers
            if fail_first:
                self.assertEqual(runtime.diagnostics[first.prefix]['failure_category'], 'timeout')
                self.assertEqual(runtime.diagnostics[second.prefix]['outcome'], 'clear')
                self.assertEqual(notices, [])
            else:
                self.assertTrue(all(value['outcome'] == 'suspicious' for value in runtime.diagnostics.values()))
                self.assertCountEqual(notices, ['Printer One print check', 'Printer Two print check'])
                self.assertCountEqual([(count, number) for count, number, _ in requests], [(1, 1), (1, 2), (3, 1), (3, 2)])
            self.assertEqual(runtime.staging.active, {})
            self.assertEqual(list(root.iterdir()), [])
            self.assertNotIn('synthetic-private', str(runtime.diagnostics))
