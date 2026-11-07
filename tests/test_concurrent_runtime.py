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


def eligible_states(settings=SETTINGS):
    states = {}
    changed = datetime.now(timezone.utc)
    for number, printer in enumerate(settings.printers, 1):
        for domain, suffix, value in (
            ('sensor', 'print_status', 'running'), ('sensor', 'current_stage', 'printing'),
            ('binary_sensor', 'online', 'on'), ('binary_sensor', 'print_error', 'off'),
            ('sensor', 'task_name', f'synthetic job {number}'),
            ('sensor', 'start_time', f'synthetic start {number}'),
            ('sensor', 'current_layer', str(number * 11)),
        ):
            states[getattr(printer,suffix)] = SimpleNamespace(state=value, last_changed=changed)
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
        ticks = {printer.key: 0.0 for printer in SETTINGS.printers}
        captures = {printer.key: 0 for printer in SETTINGS.printers}
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
                captures[printer.key] += 1
                index = captures[printer.key]
                base = 0 if printer == SETTINGS.printers[0] else 100
                return Capture(synthetic_jpeg(base + index), ticks[printer.key])

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
                self.assertEqual(runtime.diagnostics[first.key]['failure_category'], 'timeout')
                self.assertEqual(runtime.diagnostics[second.key]['outcome'], 'clear')
                self.assertEqual(notices, [])
            else:
                self.assertTrue(all(value['outcome'] == 'suspicious' for value in runtime.diagnostics.values()))
                self.assertCountEqual(notices, ['Printer One print check', 'Printer Two print check'])
                self.assertCountEqual([(count, number) for count, number, _ in requests], [(1, 1), (1, 2), (3, 1), (3, 2)])
            self.assertEqual(runtime.staging.active, {})
            self.assertEqual(list(root.iterdir()), [])
            self.assertNotIn('synthetic-private', str(runtime.diagnostics))


class ScheduledAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_printers_queue_before_capture_and_recover_after_provider_failure(self):
        from printer_guard.config import parse_config
        from test_scheduler import settled
        settings=parse_config({'ai_task':'ai_task.synthetic','notify_service':'notify.synthetic',
            'printers':[{'name':f'Printer {n}','entity_prefix':f'printer_{n}',
                'camera':f'camera.printer_{n}'} for n in range(1,6)]})
        entered=asyncio.Event(); release=asyncio.Event()
        captures=[]; requests=[]; active=0; peak=0
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            class Services:
                async def async_call(inner,domain,service,data,**kwargs):
                    nonlocal active,peak
                    self.assertEqual((domain,service),('ai_task','generate_data'))
                    telemetry=json.loads(data['instructions'].split('Telemetry is context only: ',1)[1])
                    number=int(telemetry['layer'])//11
                    paths=[root/item['media_content_id'].removeprefix('media-source://media_source/local/')
                        for item in data['attachments']]
                    self.assertEqual([path.read_bytes() for path in paths],[synthetic_jpeg(number)])
                    requests.append(number)
                    active+=1; peak=max(peak,active)
                    self.assertLessEqual(len(runtime.staging.active),2)
                    if len(requests)==2: entered.set()
                    try:
                        await release.wait()
                        if number==1: raise TimeoutError('synthetic private provider detail')
                        return {'data':{'frame_1_judgeable':True,'frame_1_score':0.1,
                            'frame_1_evidence':'synthetic clear'}}
                    finally:
                        active-=1
            hass=SimpleNamespace(states=eligible_states(settings),services=Services(),
                async_create_task=asyncio.create_task)
            runtime=Runtime(hass,root,None,settings)
            runtime.staging.startup(); runtime.started-=121
            async def capture(_hass,printer):
                import time
                number=settings.printers.index(printer)+1
                captures.append(number)
                return Capture(synthetic_jpeg(number),time.monotonic())
            with patch('printer_guard.camera_capture',capture):
                try:
                    runtime.tick(None); runtime.tick(None)
                    await asyncio.wait_for(entered.wait(),2)
                    self.assertEqual(captures,[1,2])
                    self.assertEqual(len(runtime.tasks),2)
                    self.assertEqual(len(runtime.scheduling['queue']),3)
                    self.assertTrue(all(runtime.diagnostics[p.key]['outcome']=='queued'
                        for p in settings.printers[2:]))
                    with self.assertLogs('printer_guard',level='WARNING'):
                        release.set()
                        await settled(lambda:not runtime.pending)
                    self.assertEqual(captures,[1,2,3,4,5])
                    self.assertCountEqual(requests,[1,2,3,4,5])
                    self.assertEqual(peak,2)
                    self.assertEqual(runtime.diagnostics[settings.printers[0].key]['failure_category'],'timeout')
                    self.assertTrue(all(runtime.diagnostics[p.key]['outcome']=='clear'
                        for p in settings.printers[1:]))
                finally:
                    await runtime.stop(None)
            self.assertEqual(runtime.staging.active,{})
            self.assertEqual(list(root.iterdir()),[])
            self.assertEqual(runtime.pending,set())
            self.assertEqual(runtime.scheduling['active'],set())
            self.assertNotIn('private',str(runtime.diagnostics))

    async def test_unload_during_active_media_cancels_queue_and_cleans_files(self):
        from printer_guard.config import parse_config
        settings=parse_config({'ai_task':'ai_task.synthetic','notify_service':'notify.synthetic',
            'printers':[{'name':f'Printer {n}','entity_prefix':f'printer_{n}',
                'camera':f'camera.printer_{n}'} for n in range(1,6)]})
        entered=asyncio.Event(); requests=[]; captures=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            class Services:
                async def async_call(inner,domain,service,data,**kwargs):
                    self.assertEqual((domain,service),('ai_task','generate_data'))
                    requests.append(data['attachments'])
                    if len(requests)==2: entered.set()
                    await asyncio.Event().wait()
            hass=SimpleNamespace(states=eligible_states(settings),services=Services(),
                async_create_task=asyncio.create_task)
            runtime=Runtime(hass,root,None,settings)
            runtime.staging.startup(); runtime.started-=121
            async def capture(_hass,printer):
                import time
                captures.append(printer.key)
                return Capture(synthetic_jpeg(1),time.monotonic())
            with patch('printer_guard.camera_capture',capture):
                try:
                    runtime.tick(None)
                    await asyncio.wait_for(entered.wait(),2)
                    self.assertEqual(len(runtime.staging.active),2)
                    self.assertEqual(len(runtime.scheduling['queue']),3)
                finally:
                    await runtime.stop(None)
            self.assertEqual(len(captures),2)
            self.assertEqual(len(requests),2)
            self.assertEqual(runtime.staging.active,{})
            self.assertEqual(list(root.iterdir()),[])
            self.assertEqual(runtime.tasks,set())
            self.assertEqual(runtime.pending,set())
            self.assertEqual(list(runtime.scheduling['queue']),[])
            self.assertTrue(all(value['reason']=='cancelled' for value in runtime.diagnostics.values()))
