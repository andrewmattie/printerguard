import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from printer_guard import ai_task_adapter as mod
from printer_guard.media import Staging
def staging(root):
    s=Staging(root); s.startup(); return s
class Bus:
    def __init__(self,root,fail=False): self.root=root; self.fail=fail; self.paths=[]
    async def async_call(self,domain,service,data,blocking,return_response):
        self.structure=data['structure']
        self.paths=[self.root / x['media_content_id'].split('local/')[1] for x in data['attachments']]
        assert all(p.exists() for p in self.paths)
        assert domain=='ai_task' and service=='generate_data' and return_response
        if self.fail: raise RuntimeError('failure')
        self.contents=[p.read_bytes() for p in self.paths]
        response = {
            'frame_1_judgeable': True, 'frame_1_score': 0.9, 'frame_1_evidence': 'loose filament',
            'frame_2_judgeable': False, 'frame_2_score': 0.0, 'frame_2_evidence': 'obscured',
            'frame_3_judgeable': True, 'frame_3_score': 0.4, 'frame_3_evidence': 'slight deformation',
        }
        if len(self.paths) == 1:
            response = {key: value for key, value in response.items() if key.startswith('frame_1_')}
        return {'data': response}
class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_and_comparison_tasks_are_routed_independently(self):
        for screen_task in (None, 'ai_task.example_screen'):
            with self.subTest(screen_task=screen_task), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = []
                class RoutingBus(Bus):
                    async def async_call(self, domain, service, data, **kwargs):
                        calls.append((data['entity_id'], len(data['attachments'])))
                        return await super().async_call(domain, service, data, **kwargs)
                adapter = mod.AITaskAdapter(SimpleNamespace(services=RoutingBus(root)),
                    'ai_task.example_comparison', staging(root), screen_entity_id=screen_task)
                frame = SimpleNamespace(content=b'1234')
                await adapter.screen(frame, {})
                await adapter.compare([frame] * 3, {})
                self.assertEqual(calls, [
                    (screen_task or 'ai_task.example_comparison', 1),
                    ('ai_task.example_comparison', 3),
                ])
                self.assertEqual(list(root.iterdir()), [])

    async def test_repeated_cancellation_waits_for_preparation_and_cleanup(self):
        await self.check_repeated_file_cancellation('prepare')

    async def test_repeated_cancellation_waits_for_cleanup(self):
        await self.check_repeated_file_cancellation('cleanup')

    async def check_repeated_file_cancellation(self, operation):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            stage=staging(root)
            started=asyncio.Event()
            finished=asyncio.Event()
            release=threading.Event()
            loop=asyncio.get_running_loop()
            original=getattr(stage,operation)
            def blocked(*args):
                if operation == 'prepare':
                    result=original(*args)
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5): raise TimeoutError('synthetic test gate')
                if operation == 'cleanup':
                    result=original(*args)
                loop.call_soon_threadsafe(finished.set)
                return result
            setattr(stage,operation,blocked)
            adapter=mod.AITaskAdapter(SimpleNamespace(services=Bus(root)),'ai_task.example',stage)
            task=asyncio.create_task(adapter.screen(SimpleNamespace(content=b'1234'),{}))
            try:
                await asyncio.wait_for(started.wait(),2)
                for _ in range(2):
                    task.cancel()
                    for _ in range(3): await asyncio.sleep(0)
                self.assertFalse(task.done(),'caller returned while private disk work was still running')
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                await asyncio.wait_for(finished.wait(),2)
            self.assertEqual(list(root.iterdir()),[])
            self.assertEqual(stage.active,{})

    async def test_cleanup_after_service_cancellation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); started=asyncio.Event()
            class WaitingBus:
                async def async_call(self,*args,**kwargs):
                    started.set()
                    await asyncio.Event().wait()
            a=mod.AITaskAdapter(SimpleNamespace(services=WaitingBus()),'ai_task.example',staging(root))
            task=asyncio.create_task(a.screen(SimpleNamespace(content=b'1234'),{}))
            await started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(list(root.iterdir()),[])
    async def test_success_requests_ordered_frame_verdicts_and_cleans_staging(self):
        verdicts = [
            {'judgeable': True, 'score': 0.9, 'evidence': 'loose filament'},
            {'judgeable': False, 'score': 0.0, 'evidence': 'obscured'},
            {'judgeable': True, 'score': 0.4, 'evidence': 'slight deformation'},
        ]
        selectors = {'judgeable': {'boolean': {}}, 'score': {'number': {'min': 0, 'max': 1, 'step': 0.01}},
                     'evidence': {'text': {}}}
        for count in (1, 3):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bus = Bus(root)
                adapter = mod.AITaskAdapter(SimpleNamespace(services=bus), 'ai_task.example', staging(root))
                contents = [b'1111', b'2222', b'3333'][:count]
                frames = [SimpleNamespace(content=content) for content in contents]
                if count == 1:
                    out = [await adapter.screen(frames[0], {})]
                else:
                    out = await adapter.compare(frames, {})
                self.assertEqual(out, verdicts[:count])
                self.assertEqual(bus.contents, contents)
                expected = {f'frame_{i}_{name}': selector for i in range(1, count + 1)
                            for name, selector in selectors.items()}
                self.assertEqual(set(bus.structure), set(expected))
                for name, selector in expected.items():
                    self.assertIs(bus.structure[name]['required'], True, name)
                    self.assertEqual(bus.structure[name]['selector'], selector, name)
                self.assertEqual(list(root.iterdir()), [])

    async def test_symlink_media_source_resolves_attachments_and_cleans_each_request(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            unrelated = target / 'unrelated.txt'
            unrelated.write_text('keep')
            root = Path(directory) / 'local'
            root.symlink_to(target, target_is_directory=True)
            bus = Bus(root)
            stage = staging(root)
            adapter = mod.AITaskAdapter(SimpleNamespace(services=bus), 'ai_task.example', stage)

            for content in (b'1111', b'2222'):
                await adapter.screen(SimpleNamespace(content=content), {})
                self.assertEqual(bus.contents, [content])
                self.assertEqual(list(target.iterdir()), [unrelated])
                self.assertEqual(stage.active, {})
            self.assertEqual(unrelated.read_text(), 'keep')

    async def test_cleanup_after_error(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); bus=Bus(root,True); a=mod.AITaskAdapter(SimpleNamespace(services=bus),'ai_task.example',staging(root))
            with self.assertRaises(RuntimeError): await a.screen(SimpleNamespace(content=b'1234'),{})
            self.assertEqual(list(root.iterdir()),[])
            self.assertEqual(a.diagnostic_phase,'ai_service')
    async def test_failed_staging_and_cleanup_have_distinct_phases(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            class FailPrepare:
                def __init__(self): self.root=root
                def prepare(self,frames): raise PermissionError('secret path')
            a=mod.AITaskAdapter(SimpleNamespace(services=Bus(root)),'ai_task.example',FailPrepare())
            with self.assertRaises(PermissionError): await a.screen(SimpleNamespace(content=b'1234'),{})
            self.assertEqual(a.diagnostic_phase,'media_prepare')
            stage=staging(root)
            def cleanup(_): raise OSError('secret path')
            stage.cleanup=cleanup
            a=mod.AITaskAdapter(SimpleNamespace(services=Bus(root)),'ai_task.example',stage)
            with self.assertRaises(OSError): await a.screen(SimpleNamespace(content=b'1234'),{})
            self.assertEqual(a.diagnostic_phase,'media_cleanup')
    async def test_invalid_service_response_phase(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            class InvalidBus:
                async def async_call(self,*args,**kwargs): return {'data':None}
            a=mod.AITaskAdapter(SimpleNamespace(services=InvalidBus()),'ai_task.example',staging(root))
            with self.assertRaises(ValueError): await a.screen(SimpleNamespace(content=b'1234'),{})
            self.assertEqual(a.diagnostic_phase,'ai_response')
            self.assertEqual(list(root.iterdir()),[])
    def test_explicit_task_required(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): mod.AITaskAdapter(None,'',staging(Path(d)))
if __name__=='__main__': unittest.main()
