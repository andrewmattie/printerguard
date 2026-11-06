import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest

p = Path(__file__).parents[1] / 'custom_components' / 'p1s_guard' / 'engine.py'
spec = importlib.util.spec_from_file_location('p1s_engine', p)
e = importlib.util.module_from_spec(spec); sys.modules[spec.name] = e; spec.loader.exec_module(e)

class Fake:
    def __init__(self):
        self.t = 0.0; self.n = 0; self.called = []; self.status = 'running'; self.stage='printing'; self.job='job'; self.epoch='epoch'; self.trusted=False
        self.result={'judgeable':True, 'score':0.9, 'evidence':'loose filament'}; self.same=False; self.notify_error=False
    def ctx(self): return e.Context(self.status,self.stage,True,True,'off',self.job,'start',self.epoch,{'layer':None})
    def clock(self): return self.t
    async def sleep(self,n): self.t += n
    async def capture(self):
        self.n += 1; data=b'\xff\xd8'+(b'a' if self.same else bytes([self.n]))+b'\xff\xd9'
        return e.Capture(data,self.t,str(self.n) if self.trusted else None)
    async def screen(self, frame, ctx): self.called.append('screen'); return self.result
    async def compare(self, frames, ctx): self.called.append('compare'); return [self.result]*3
    async def reserve(self,task,start): self.called.append('reserve'); return True
    async def pause(self): self.called.append('pause'); self.status='pause'
    async def notify(self,msg): self.called.append(('notify',msg));
    def monitor(self, **kwargs):
        return e.Monitor(capture=self.capture,screen=self.screen,compare=self.compare,context=self.ctx,reserve=self.reserve,pause=self.pause,notify=self.notify,clock=self.clock,sleep=self.sleep,**kwargs)

class TestEngine(unittest.IsolatedAsyncioTestCase):
    async def test_screen_clear_no_more_calls(self):
        f=Fake(); f.result={'judgeable':True,'score':.2,'evidence':'normal'}
        self.assertEqual(await f.monitor().run(),'screen_clear_or_unjudgeable'); self.assertEqual(f.n,1)
    async def test_three_frames_dry_run_never_pauses(self):
        f=Fake(); m=f.monitor(); self.assertEqual(await m.run(),'monitor_only')
        self.assertEqual(f.n,3); self.assertNotIn('pause',f.called); self.assertEqual(f.t,60)
    async def test_untrusted_cannot_pause_even_if_armed(self):
        f=Fake(); self.assertEqual(await f.monitor(allow_pause=True).run(),'monitor_only'); self.assertNotIn('pause',f.called)
    async def test_duplicate_aborts(self):
        f=Fake(); f.same=True; self.assertEqual(await f.monitor().run(),'unavailable_or_error'); self.assertNotIn('compare',f.called)
    async def test_stage_change_aborts(self):
        f=Fake()
        async def s(n): f.t+=n; f.stage='changing_filament'
        f.sleep=s; self.assertEqual(await f.monitor().run(),'session_changed'); self.assertNotIn('pause',f.called)
    async def test_invalid_response_fails_closed(self):
        f=Fake(); f.result={'judgeable':True,'score':'0.9','evidence':'x'}
        self.assertEqual(await f.monitor().run(),'unavailable_or_error'); self.assertNotIn('pause',f.called)
    async def test_pause_requires_trusted_ids_and_confirmed_state(self):
        f=Fake(); f.trusted=True
        self.assertEqual(await f.monitor(allow_pause=True).run(),'paused_confirmed')
        self.assertIn('reserve',f.called); self.assertIn('pause',f.called)
    async def test_pause_error_notifies_without_ai(self):
        f=Fake(); f.trusted=True
        async def fail(): raise RuntimeError('failure')
        f.pause=fail; self.assertEqual(await f.monitor(allow_pause=True).run(),'pause_uncertain')
        self.assertTrue(any(isinstance(x,tuple) and 'uncertain' in x[1] for x in f.called))
    async def test_latched_job_not_repaused(self):
        f=Fake(); f.trusted=True
        async def no(*_): return False
        f.reserve=no
        self.assertEqual(await f.monitor(allow_pause=True).run(),'already_attempted'); self.assertNotIn('pause',f.called)
    async def test_compare_failure_no_pause(self):
        f=Fake()
        async def fail(*_): raise TimeoutError()
        f.compare=fail
        self.assertEqual(await f.monitor().run(),'unavailable_or_error'); self.assertNotIn('pause',f.called)
    async def test_malformed_later_comparison_is_failure_even_after_clear_result(self):
        f=Fake()
        async def compare(*_): return [{'judgeable':True,'score':0.1,'evidence':'clear'}, {}, f.result]
        f.compare=compare
        self.assertEqual(await f.monitor().run(),'unavailable_or_error')
        self.assertNotIn('pause',f.called)
    async def test_failure_diagnostics_are_bounded_and_redacted(self):
        secret='token=secret /private/camera prompt and model response'
        f=Fake()
        async def broken_camera(): raise PermissionError(secret)
        f.capture=broken_camera; m=f.monitor()
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'camera_capture','category':'permission'})
        self.assertNotIn(secret,str(m.last_error))
        self.assertNotIn('pause',f.called)
        f=Fake()
        async def bad_frame(): return e.Capture(b'not-jpeg',f.t,None)
        f.capture=bad_frame; m=f.monitor()
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'frame_validation','category':'invalid_data','reason':'invalid_jpeg_markers'})
        f=Fake()
        async def broken_screen(*_): raise TimeoutError(secret)
        f.screen=broken_screen; m=f.monitor(ai_phase=lambda:'ai_service')
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'ai_service','category':'timeout'})
        f=Fake(); f.result={'judgeable':True,'score':secret,'evidence':secret}; m=f.monitor()
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'screen_verdict','category':'invalid_data'})
        self.assertNotIn(secret,str(m.last_error))
    async def test_untrusted_phase_and_exception_name_are_not_exposed(self):
        f=Fake()
        async def fail(*_): raise RuntimeError('credential=secret')
        f.screen=fail; m=f.monitor(ai_phase=lambda:'https://secret.example/path')
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'ai_other','category':'other'})
    async def test_hung_notification_times_out_and_next_check_can_run(self):
        f=Fake(); started=asyncio.Event(); active=[0]
        async def hung(_):
            active[0]+=1; started.set()
            try: await asyncio.Event().wait()
            finally: active[0]-=1
        f.notify=hung
        monitor=f.monitor(notification_timeout=0.01)
        task=asyncio.create_task(monitor.run())
        await started.wait()
        self.assertEqual(await monitor.run(),'busy')
        self.assertEqual(await asyncio.wait_for(task,0.5),'unavailable_or_error')
        self.assertEqual(monitor.last_error,{'phase':'notification','category':'timeout'})
        self.assertFalse(monitor.lock.locked()); self.assertEqual(active[0],0)
        async def delivered(message): f.called.append(('notify',message))
        monitor.notify=delivered
        self.assertEqual(await monitor.run(),'monitor_only')
        self.assertFalse(monitor.lock.locked())
        self.assertNotIn('pause',f.called)

    async def test_cancel_during_notification_releases_lock_and_callback(self):
        f=Fake(); started=asyncio.Event(); cancelled=asyncio.Event()
        async def hung(_):
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        f.notify=hung; monitor=f.monitor()
        task=asyncio.create_task(monitor.run()); await started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cancelled.is_set()); self.assertFalse(monitor.lock.locked())
        self.assertNotIn('pause',f.called)

    async def test_notification_error_has_safe_phase(self):
        f=Fake()
        async def notify(_): raise OSError('private payload')
        f.notify=notify; m=f.monitor()
        self.assertEqual(await m.run(),'unavailable_or_error')
        self.assertEqual(m.last_error,{'phase':'notification','category':'os_error'})
        self.assertNotIn('pause',f.called)

    async def test_frame_failure_reasons_do_not_require_or_expose_real_images(self):
        jpeg = b'\xff\xd8example\xff\xd9'
        cases = [
            (None, 'missing_frame'),
            (e.Capture('synthetic-private-bytes', 0), 'invalid_frame_bytes'),
            (e.Capture(b'x', 0), 'frame_too_small'),
            (e.Capture(jpeg * 3, 0), 'frame_too_large'),
            (e.Capture(b'synthetic-private-bytes', 0), 'invalid_jpeg_markers'),
            (e.Capture(jpeg, float('nan')), 'invalid_capture_timestamp'),
            (e.Capture(jpeg, float('inf')), 'invalid_capture_timestamp'),
            (e.Capture(jpeg, 'synthetic-private-time'), 'invalid_capture_timestamp'),
            (e.Capture(jpeg, 1), 'future_capture'),
            (e.Capture(jpeg, -6), 'stale_capture'),
        ]
        for frame, reason in cases:
            with self.subTest(reason=reason):
                f = Fake()
                async def capture(value=frame): return value
                f.capture = capture
                monitor = f.monitor(max_frame_bytes=24)
                self.assertEqual(await monitor.run(), 'unavailable_or_error')
                self.assertEqual(monitor.last_error, {
                    'phase': 'frame_validation', 'category': 'invalid_data', 'reason': reason,
                })
                self.assertNotIn('screen', f.called)
                self.assertNotIn('pause', f.called)
                self.assertNotIn('synthetic-private', str(monitor.last_error))

    async def test_duplicate_frame_reports_specific_reason_and_still_aborts(self):
        f = Fake(); f.same = True
        monitor = f.monitor()
        self.assertEqual(await monitor.run(), 'unavailable_or_error')
        self.assertEqual(monitor.last_error['reason'], 'duplicate_frame')
        self.assertNotIn('compare', f.called)
        self.assertNotIn('pause', f.called)

    def test_frame_failure_reason_must_be_allowlisted(self):
        with self.assertRaises(ValueError) as result:
            e.FrameValidationError('synthetic-private-data')
        self.assertNotIn('synthetic-private-data', str(result.exception))

class ExtraTests(unittest.IsolatedAsyncioTestCase):
    async def test_intervening_state_transition_aborts(self):
        from dataclasses import replace
        f=Fake(); f.trusted=True; epoch=['before']; original=f.ctx
        f.ctx=lambda: replace(original(), guard_epoch=(epoch[0],))
        async def compare(*_):
            epoch[0]='after'
            return [f.result]*3
        f.compare=compare
        self.assertEqual(await f.monitor(allow_pause=True).run(),'session_changed')
        self.assertNotIn('pause',f.called)
    async def test_session_changes_during_comparison(self):
        f=Fake(); f.trusted=True
        async def compare(*_):
            f.epoch='next-print'
            return [f.result]*3
        f.compare=compare
        self.assertEqual(await f.monitor(allow_pause=True).run(),'session_changed')
        self.assertNotIn('pause',f.called)
    async def test_unconfirmed_pause_message(self):
        f=Fake(); f.trusted=True
        async def pause(): f.called.append('pause')
        f.pause=pause
        self.assertEqual(await f.monitor(allow_pause=True).run(),'pause_unconfirmed')
        self.assertTrue(any(isinstance(x,tuple) and 'not confirmed' in x[1] for x in f.called))
    async def test_reservation_failure_notifies(self):
        f=Fake(); f.trusted=True
        async def reserve(*_): raise RuntimeError('storage failed')
        f.reserve=reserve
        self.assertEqual(await f.monitor(allow_pause=True).run(),'reservation_failed')
        self.assertNotIn('pause',f.called)
        self.assertTrue(any(isinstance(x,tuple) and 'no pause' in x[1] for x in f.called))


if __name__ == '__main__': unittest.main()
