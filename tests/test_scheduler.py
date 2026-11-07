"""Deterministic admission, fairness and cancellation checks with synthetic work."""
import asyncio
from collections import deque
from dataclasses import replace
from pathlib import Path
import sys
import types
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from printer_guard import Runtime
from printer_guard.config import parse_config
from printer_guard.media import MAX_FRAME_BYTES, Staging
from test_engine import Fake


def fleet(count):
    return parse_config({'ai_task':'ai_task.synthetic','notify_service':'notify.synthetic',
        'printers':[{'name':f'Printer {number}','entity_prefix':f'printer_{number}',
            'camera':f'camera.printer_{number}'} for number in range(count)]})


async def settled(predicate):
    """Yield event-loop turns, failing promptly rather than waiting on wall time."""
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_fairness_above_budget_and_repeated_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(SimpleNamespace(async_create_task=asyncio.create_task),
                Path(directory),None,fleet(7))
            runtime.started-=121
            starts=[]
            releases={name:asyncio.Event() for name in runtime.jobs}
            for name in runtime.jobs:
                async def work(key=name):
                    starts.append(key)
                    await releases[key].wait()
                    return 'screen_clear'
                runtime.jobs[name]=SimpleNamespace(lock=asyncio.Lock(),run=work)
            names=list(runtime.jobs)
            try:
                runtime.tick(None); runtime.tick(None)
                await settled(lambda:len(starts)==2)
                self.assertEqual(starts,names[:2])
                self.assertEqual(len(runtime.tasks),2)
                self.assertEqual(len(runtime.scheduling['queue']),5)
                self.assertEqual(runtime.pending,set(names))
                for name in names[2:]:
                    self.assertEqual(runtime.diagnostics[name]['outcome'],'queued')
                    self.assertEqual(runtime.diagnostics[name]['reason'],'capacity')
                releases[names[0]].set()
                await settled(lambda:len(starts)==3)
                runtime.tick(None)  # The finished first printer rejoins at the tail.
                self.assertEqual(runtime.scheduling['queue'][-1][1],names[0])
                for index in range(1,7):
                    releases[names[index]].set()
                    await settled(lambda i=index:len(starts)>=min(i+3,8))
                    self.assertLessEqual(len(runtime.tasks),2)
                await settled(lambda:not runtime.pending)
                self.assertEqual(starts,names+[names[0]])
            finally:
                await runtime.stop(None)
            self.assertEqual(runtime.tasks,set())
            self.assertEqual(runtime.scheduling['active'],set())
            self.assertEqual(list(runtime.scheduling['queue']),[])

    async def test_stop_removes_only_own_queue_and_cancels_own_work(self):
        with tempfile.TemporaryDirectory() as directory:
            staging=Staging(Path(directory))
            scheduling={'queue':deque(),'active':set()}
            runtimes=[]; starts=[]
            release=asyncio.Event()
            for number,printer in enumerate(fleet(5).printers):
                runtime=Runtime(SimpleNamespace(async_create_task=asyncio.create_task),
                    Path(directory),None,replace(fleet(1),printers=(printer,)),staging,scheduling)
                async def work(key=number):
                    starts.append(key)
                    await release.wait()
                    return 'screen_clear'
                runtime.jobs[printer.key]=SimpleNamespace(lock=asyncio.Lock(),run=work)
                runtime.started-=121
                runtimes.append(runtime)
            try:
                for runtime in runtimes: runtime.tick(None)
                await settled(lambda:starts==[0,1])
                await runtimes[4].stop(None)  # Remove a queued source.
                self.assertEqual(runtimes[4].pending,set())
                self.assertEqual(next(iter(runtimes[4].diagnostics.values()))['reason'],'cancelled')
                await runtimes[0].stop(None)  # Cancel an active source and free its slot.
                await settled(lambda:starts==[0,1,2])
                self.assertEqual(next(iter(runtimes[0].diagnostics.values()))['reason'],'cancelled')
                release.set()
                await settled(lambda:not scheduling['active'] and not scheduling['queue'])
                self.assertEqual(starts,[0,1,2,3])
                self.assertEqual(runtimes[1].diagnostics[next(iter(runtimes[1].jobs))]['outcome'],'clear')
            finally:
                for runtime in runtimes: await runtime.stop(None)
            self.assertTrue(all(not runtime.tasks and not runtime.pending for runtime in runtimes))
            self.assertEqual(scheduling,{'queue':deque(),'active':set()})

    async def test_queued_work_revalidates_eligibility_before_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(SimpleNamespace(async_create_task=asyncio.create_task),
                Path(directory),None,fleet(3))
            release=asyncio.Event()
            names=list(runtime.jobs)
            async def held():
                await release.wait()
                return 'screen_clear'
            for name in names[:2]:
                runtime.jobs[name]=SimpleNamespace(lock=asyncio.Lock(),run=held)
            fake=Fake(); runtime.jobs[names[2]]=fake.monitor()
            runtime.started-=121
            try:
                runtime.tick(None)
                self.assertEqual(fake.n,0)
                fake.status='idle'
                release.set()
                await settled(lambda:not runtime.pending)
                self.assertEqual(fake.n,0)
                self.assertEqual(fake.called,[])
                self.assertEqual(runtime.diagnostics[names[2]]['reason'],'printer_ineligible')
            finally:
                await runtime.stop(None)

    async def test_queued_work_captures_new_session_and_receipt_after_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(SimpleNamespace(async_create_task=asyncio.create_task),
                Path(directory),None,fleet(3))
            release=asyncio.Event(); names=list(runtime.jobs)
            async def held():
                await release.wait()
                return 'screen_clear'
            for name in names[:2]:
                runtime.jobs[name]=SimpleNamespace(lock=asyncio.Lock(),run=held)
            fake=Fake(); fake.result={'judgeable':True,'score':0.1,'evidence':'synthetic'}
            received=[]
            async def capture():
                frame=await fake.capture()
                received.append(frame.received_at)
                return frame
            monitor=fake.monitor(); monitor.capture=capture
            runtime.jobs[names[2]]=monitor
            runtime.started-=121
            try:
                runtime.tick(None)
                self.assertEqual(fake.n,0)
                fake.job='replacement synthetic job'; fake.epoch='replacement epoch'; fake.t=1000
                release.set()
                await settled(lambda:not runtime.pending)
                self.assertEqual(received,[1000])
                self.assertEqual(monitor.history_job,('replacement synthetic job','start'))
                self.assertEqual(runtime.diagnostics[names[2]]['outcome'],'clear')
                self.assertEqual(fake.called,['screen'])
            finally:
                await runtime.stop(None)

    async def test_unchanged_idle_fleet_keeps_diagnostic_write_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(SimpleNamespace(async_create_task=asyncio.create_task),
                Path(directory),None,fleet(5))
            observed=[]
            runtime.observers.append(observed.append)
            for name in runtime.jobs:
                fake=Fake(); fake.status='idle'
                runtime.jobs[name]=fake.monitor()
            runtime.started-=121
            try:
                runtime.tick(None)
                await settled(lambda:not runtime.pending)
                self.assertEqual(len(observed),5)
                timestamps={name:value['last_check'] for name,value in runtime.diagnostics.items()}
                runtime.tick(None)
                await settled(lambda:not runtime.pending)
                self.assertEqual(len(observed),5)
                self.assertEqual({name:value['last_check'] for name,value in runtime.diagnostics.items()},timestamps)
            finally:
                await runtime.stop(None)

    async def test_home_assistant_stop_disables_dispatch_without_double_unsubscribe(self):
        modules={name:types.ModuleType(name) for name in ('homeassistant','homeassistant.const',
            'homeassistant.core','homeassistant.helpers','homeassistant.helpers.event')}
        modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP='stop'
        modules['homeassistant.core'].callback=lambda function:function
        unsubscribed=[]
        modules['homeassistant.helpers.event'].async_track_time_interval=(
            lambda *args:lambda:unsubscribed.append('timer'))
        class Bus:
            removed=False
            def async_listen_once(inner,event,callback):
                inner.listener=callback
                def remove():
                    self.assertFalse(inner.removed,'one-shot listener was already removed by HA')
                    inner.removed=True
                return remove
        bus=Bus()
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(SimpleNamespace(bus=bus,async_create_task=asyncio.create_task),
                Path(directory),None,fleet(3))
            with patch.dict(sys.modules,modules):
                runtime.start()
                bus.removed=True  # Core removes its one-shot listener before invoking it.
                await bus.listener(None)
            self.assertTrue(runtime.stopping)
            self.assertTrue(runtime.scheduling['stopping'])
            self.assertEqual(unsubscribed,['timer'])
            runtime.started-=121
            runtime.tick(None)
            self.assertEqual(runtime.pending,set())
            self.assertEqual(runtime.tasks,set())

    async def test_full_size_comparisons_fit_every_admitted_media_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            staging=Staging(Path(directory)); staging.startup()
            frame=SimpleNamespace(content=b'x'*MAX_FRAME_BYTES)
            paths=[]
            try:
                for _ in range(staging.MAX_ACTIVE):
                    path,_=staging.prepare([frame]*3)
                    paths.append(path)
                self.assertEqual(len(staging.active),staging.MAX_ACTIVE)
                with self.assertRaisesRegex(RuntimeError,'staging quota exceeded'):
                    staging.prepare([frame])
            finally:
                for path in paths: staging.cleanup(path)
            self.assertEqual(staging.active,{})
            self.assertEqual(list(Path(directory).iterdir()),[])
