import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from p1s_guard import ai_task_adapter as mod
from p1s_guard.media import Staging
def staging(root):
    s=Staging(root); s.startup(); return s
class Bus:
    def __init__(self,root,fail=False): self.root=root; self.fail=fail; self.paths=[]
    async def async_call(self,domain,service,data,blocking,return_response):
        self.paths=[self.root / x['media_content_id'].split('local/')[1] for x in data['attachments']]
        assert all(p.exists() for p in self.paths)
        assert domain=='ai_task' and service=='generate_data' and return_response
        if self.fail: raise RuntimeError('failure')
        return {'data':{f'frame_{i}_{k}':v for i in range(1,len(self.paths)+1) for k,v in [('judgeable',True),('score',.9),('evidence','visible')]}}
class Tests(unittest.IsolatedAsyncioTestCase):
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
    async def test_three_files_cleaned_after_success(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); bus=Bus(root); a=mod.AITaskAdapter(SimpleNamespace(services=bus),'ai_task.example',staging(root))
            out=await a.compare([SimpleNamespace(content=b'1234')]*3,{})
            self.assertEqual(len(out),3); self.assertTrue(all(not p.exists() for p in bus.paths)); self.assertEqual(list(root.iterdir()),[])
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
