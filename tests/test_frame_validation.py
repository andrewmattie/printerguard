"""Safe reproductions of camera response shapes, not evidence from a live camera."""
from pathlib import Path
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from p1s_guard.engine import Context, FrameValidationError, Monitor
from p1s_guard.ha_runtime import camera_capture
from guard_fixtures import SETTINGS, synthetic_jpeg


class FrameValidationTests(unittest.IsolatedAsyncioTestCase):
    def camera_module(self, content, content_type='image/jpeg'):
        module=types.ModuleType('homeassistant.components.camera')
        async def get_image(hass, entity_id):
            self.assertEqual(entity_id, SETTINGS.printers[0].camera)
            return SimpleNamespace(content=content,content_type=content_type)
        module.async_get_image=get_image
        return module

    def monitor(self):
        async def capture(): return await camera_capture(None,SETTINGS.printers[0])
        async def forbidden(*args): self.fail('unexpected comparison, reservation or control action')
        async def screen(*args): return {'judgeable':True,'score':0.9,'evidence':'synthetic assessment'}
        async def no_wait(*args): pass
        context=Context('running','printing',True,True,'off','example job','example start','epoch',{})
        return Monitor(capture=capture,screen=screen,compare=forbidden,context=lambda:context,
                       reserve=forbidden,pause=forbidden,notify=forbidden,clock=time.monotonic,sleep=no_wait)

    async def test_complete_synthetic_jpeg_passes_initial_frame_gate(self):
        content=synthetic_jpeg()
        with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(content)}):
            frame=await self.monitor().get_frame(set())
        self.assertEqual(frame.content,content)
        self.assertIsNone(frame.source_frame_id)

    async def test_identical_camera_responses_reproduce_duplicate_frame_failure(self):
        with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(synthetic_jpeg())}):
            monitor=self.monitor()
            self.assertEqual(await monitor.run(),'unavailable_or_error')
        self.assertEqual(monitor.last_error,{
            'phase':'frame_validation','category':'invalid_data','reason':'duplicate_frame',
        })

    async def test_changed_encoded_bytes_still_supply_no_trusted_source_id(self):
        seen=set()
        frames=[]
        monitor=self.monitor()
        for quantization in (1,2):
            module=self.camera_module(synthetic_jpeg(quantization))
            with patch.dict(sys.modules,{'homeassistant.components.camera':module}):
                frames.append(await monitor.get_frame(seen))
        self.assertNotEqual(frames[0].content,frames[1].content)
        self.assertEqual([frame.source_frame_id for frame in frames],[None,None])

    async def test_truncated_or_wrapped_camera_bytes_are_rejected_without_normalizing(self):
        for content in (synthetic_jpeg()[:-2],synthetic_jpeg()+b'\r\n',b'--synthetic-boundary\r\n'+synthetic_jpeg()):
            with self.subTest(length=len(content)):
                with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(content)}):
                    monitor=self.monitor()
                    self.assertEqual(await monitor.run(),'unavailable_or_error')
                self.assertEqual(monitor.last_error,{
                    'phase':'frame_validation','category':'invalid_data','reason':'invalid_jpeg_markers',
                })

    async def test_non_jpeg_declared_format_stays_rejected(self):
        for content_type in ('image/png','image/jpg','image/jpeg; charset=binary'):
            with self.subTest(content_type=content_type):
                with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(synthetic_jpeg(),content_type)}):
                    with self.assertRaises(FrameValidationError) as result:
                        await self.monitor().get_frame(set())
                self.assertEqual(result.exception.reason,'unsupported_image_format')


if __name__ == '__main__':
    unittest.main()
