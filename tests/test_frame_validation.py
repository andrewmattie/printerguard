"""Safe reproductions of camera response shapes, not evidence from a live camera."""
from pathlib import Path
import sys
import time
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from printer_guard.engine import MAX_FRAME_BYTES, Context, FrameValidationError, Monitor
from printer_guard.ha_runtime import camera_capture
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

    async def test_mutable_camera_jpeg_is_frozen_before_validation(self):
        original=synthetic_jpeg()
        content=bytearray(original)
        with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(content)}):
            frame=await self.monitor().get_frame(set())
        content[:]=b'changed camera buffer'
        self.assertIs(type(frame.content),bytes)
        self.assertEqual(frame.content,original)
        self.assertIsNone(frame.source_frame_id)

    async def test_oversized_mutable_camera_buffer_is_rejected_before_copying(self):
        module=self.camera_module(bytearray(MAX_FRAME_BYTES+1))
        with patch.dict(sys.modules,{'homeassistant.components.camera':module}), patch(
                'printer_guard.ha_runtime.bytes',side_effect=AssertionError('copy must not run'),create=True):
            monitor=self.monitor()
            self.assertEqual(await monitor.run(),'unavailable_or_error')
        self.assertEqual(monitor.last_error,{
            'phase':'camera_capture','category':'invalid_data','reason':'frame_too_large',
        })

    async def test_mutable_camera_buffer_still_requires_a_decodable_jpeg(self):
        module=self.camera_module(bytearray(b'\xff\xd8synthetic invalid JPEG\xff\xd9'))
        with patch.dict(sys.modules,{'homeassistant.components.camera':module}):
            monitor=self.monitor()
            self.assertEqual(await monitor.run(),'unavailable_or_error')
        self.assertEqual(monitor.last_error,{
            'phase':'frame_validation','category':'invalid_data','reason':'invalid_jpeg_data',
        })

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

    async def test_marker_only_and_truncated_scan_header_never_reach_ai(self):
        for content in (b'\xff\xd8\xff\xd9', b'\xff\xd8private decoder payload\xff\xd9',
                        synthetic_jpeg()[:-4]+b'\xff\xd9'):
            with self.subTest(length=len(content)):
                with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(content)}):
                    monitor=self.monitor()
                    self.assertEqual(await monitor.run(),'unavailable_or_error')
                self.assertEqual(monitor.last_error,{
                    'phase':'frame_validation','category':'invalid_data','reason':'invalid_jpeg_data',
                })
                self.assertNotIn('private',str(monitor.last_error))

    async def test_dimension_and_pixel_limits_precede_decode(self):
        for width,height,reason in ((4097,8,'frame_dimensions_exceeded'),
                                    (8,4097,'frame_dimensions_exceeded'),
                                    (4096,4096,'frame_pixel_limit_exceeded')):
            with self.subTest(width=width,height=height):
                module=self.camera_module(synthetic_jpeg(width=width,height=height))
                with patch.dict(sys.modules,{'homeassistant.components.camera':module}):
                    monitor=self.monitor()
                    self.assertEqual(await monitor.run(),'unavailable_or_error')
                self.assertEqual(monitor.last_error['reason'],reason)

    async def test_supported_jpeg_modes_and_limit_boundary_decode(self):
        from PIL import Image
        from io import BytesIO
        for mode,size,options in (('RGB',(16,16),{}),('L',(4096,8),{}),('RGB',(16,16),{'progressive':True})):
            with self.subTest(mode=mode,size=size,options=options):
                data=BytesIO()
                Image.new(mode,size).save(data,format='JPEG',**options)
                module=self.camera_module(data.getvalue())
                with patch.dict(sys.modules,{'homeassistant.components.camera':module}):
                    frame=await self.monitor().get_frame(set())
                self.assertEqual(frame.content,data.getvalue())

    async def test_decode_delay_cannot_send_an_old_receipt_to_ai(self):
        from test_engine import Fake
        fake=Fake()
        def delayed_decode(content): fake.t+=6
        with patch('printer_guard.engine.verify_jpeg',delayed_decode):
            monitor=fake.monitor()
            self.assertEqual(await monitor.run(),'unavailable_or_error')
        self.assertEqual(monitor.last_error['reason'],'stale_receipt')
        self.assertNotIn('screen',fake.called)

    async def test_decoding_runs_off_the_event_loop(self):
        from printer_guard.frame_validation import verify_jpeg
        main_thread=threading.get_ident(); worker_threads=[]
        def verify(content):
            worker_threads.append(threading.get_ident())
            return verify_jpeg(content)
        with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(synthetic_jpeg())}), patch('printer_guard.engine.verify_jpeg',verify):
            await self.monitor().get_frame(set())
        self.assertEqual(len(worker_threads),1)
        self.assertNotEqual(worker_threads[0],main_thread)

    async def test_non_jpeg_declared_format_stays_rejected(self):
        for content_type in ('image/png','image/jpg','image/jpeg; charset=binary'):
            with self.subTest(content_type=content_type):
                with patch.dict(sys.modules,{'homeassistant.components.camera':self.camera_module(synthetic_jpeg(),content_type)}):
                    with self.assertRaises(FrameValidationError) as result:
                        await self.monitor().get_frame(set())
                self.assertEqual(result.exception.reason,'unsupported_image_format')


if __name__ == '__main__':
    unittest.main()
