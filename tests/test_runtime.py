import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import types

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from p1s_guard import CONFIG_SCHEMA, Runtime, async_setup, diagnostic_outcome
from p1s_guard.config import parse_config
from p1s_guard.engine import Capture
from guard_fixtures import SETTINGS, example_config, synthetic_jpeg
from p1s_guard.latch import Latch
from p1s_guard.media import prepare, recover, Staging

class FakeStore:
    def __init__(self): self.data=None; self.fail=False
    async def async_load(self): return self.data
    async def async_save(self,value):
        if self.fail: raise OSError('failure')
        self.data=value.copy()

class Tests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_quota_and_fail_closed_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); stage=Staging(root); stage.startup()
            frame=SimpleNamespace(content=b'1234')
            first,_=stage.prepare([frame]); second,_=stage.prepare([frame])
            with self.assertRaises(RuntimeError): stage.prepare([frame])
            stage.cleanup(first)
            (second/'other.txt').write_text('keep')
            with self.assertRaises(ValueError): stage.cleanup(second)
            with self.assertRaises(RuntimeError): stage.prepare([frame])
            self.assertTrue((second/'other.txt').exists())
    async def test_byte_quota_is_global(self):
        with tempfile.TemporaryDirectory() as d:
            stage=Staging(Path(d)); stage.startup(); stage.MAX_BYTES=30
            with self.assertRaises(RuntimeError):
                stage.prepare([SimpleNamespace(content=b'x'*16)])
            self.assertEqual(list(Path(d).iterdir()),[])
    async def test_concurrent_writers_cannot_exceed_global_limit(self):
        with tempfile.TemporaryDirectory() as d:
            stage=Staging(Path(d)); stage.startup()
            frame=SimpleNamespace(content=b'1234')
            async def attempt():
                try: return await asyncio.to_thread(stage.prepare,[frame])
                except RuntimeError: return None
            out=await asyncio.gather(*(attempt() for _ in range(12)))
            successes=[result for result in out if result is not None]
            self.assertEqual(len(successes),2)
            for directory,_ in successes: stage.cleanup(directory)
            self.assertEqual(list(Path(d).iterdir()),[])
    async def test_recovery_failure_keeps_staging_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); directory=root/'p1s_guard_tmp_abcdefgh'; directory.mkdir()
            stage=Staging(root)
            with self.assertRaises(ValueError): stage.startup()
            with self.assertRaises(RuntimeError): stage.prepare([SimpleNamespace(content=b'1234')])
    async def test_setup_recovers_files_before_scheduling_and_registers_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); directory,_=prepare(root,[SimpleNamespace(content=b'1234')])
            events=[]
            def schedule(hass,callback,interval):
                self.assertFalse(directory.exists())
                events.append(('schedule',interval.total_seconds()))
                return lambda: events.append(('unsub',))
            class Store(FakeStore):
                def __init__(self,*args): super().__init__()
            class Bus:
                def async_listen_once(self,event,callback): events.append(('stop_listener',event))
            h=SimpleNamespace(config=SimpleNamespace(media_dirs={'local':d}),data={},bus=Bus())
            modules={name:types.ModuleType(name) for name in ('homeassistant','homeassistant.const','homeassistant.core','homeassistant.helpers','homeassistant.helpers.event','homeassistant.helpers.storage','homeassistant.helpers.discovery')}
            modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP='stop'
            modules['homeassistant.core'].callback=lambda function: function
            modules['homeassistant.helpers.event'].async_track_time_interval=schedule
            modules['homeassistant.helpers.storage'].Store=Store
            async def load(*args): events.append(('load',))
            modules['homeassistant.helpers.discovery'].async_load_platform=load
            h.async_create_task=asyncio.create_task
            with patch.dict(sys.modules,modules):
                self.assertTrue(await async_setup(h,{'p1s_guard':example_config()}))
                await asyncio.sleep(0)
            self.assertEqual(events,[('schedule',60),('stop_listener','stop'),('load',)])
            await h.data['p1s_guard'].stop(None)
            self.assertEqual(events[-1],('unsub',))
    async def test_durable_latch_is_per_printer_and_fail_closed(self):
        store=FakeStore(); latch=Latch(store); await latch.load()
        self.assertTrue(await latch.reserve('p1s','same','start'))
        self.assertTrue(await latch.reserve('a1','same','start'))
        restored=Latch(store); await restored.load()
        self.assertFalse(await restored.reserve('p1s','same','start'))
        self.assertFalse(await restored.reserve('a1','same','start'))
        store.fail=True
        with self.assertRaises(OSError): await restored.reserve('p1s','other','start')
        self.assertFalse(await restored.reserve('p1s','same','start'))
    async def test_concurrent_latch_reserves_once(self):
        store=FakeStore(); latch=Latch(store); await latch.load()
        out=await asyncio.gather(*(latch.reserve('p1s','job','start') for _ in range(5)))
        self.assertEqual(out.count(True),1)
    async def test_startup_recovery_removes_only_owned_staging(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); other=root/'other.txt'; other.write_text('keep')
            directory,paths=prepare(root,[SimpleNamespace(content=b'1234')])
            self.assertTrue(paths[0].exists()); recover(root)
            self.assertFalse(directory.exists()); self.assertEqual(other.read_text(),'keep')
    async def test_cleanup_refuses_unexpected_content(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); directory,_=prepare(root,[SimpleNamespace(content=b'1234')])
            (directory/'other.txt').write_text('keep')
            with self.assertRaises(ValueError): recover(root)
            self.assertTrue((directory/'other.txt').exists())
    async def test_recovery_does_not_delete_matching_unowned_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); directory=root/'p1s_guard_tmp_abcdefgh'; directory.mkdir()
            (directory/'frame_1.jpg').write_bytes(b'other')
            with self.assertRaises(ValueError): recover(root)
            self.assertEqual((directory/'frame_1.jpg').read_bytes(),b'other')
    async def test_runtime_isolates_monitors_and_stops_tasks(self):
        class Hass:
            def __init__(self): self.services=SimpleNamespace(); self.states={}
            def async_create_task(self,coro): return asyncio.create_task(coro)
        with tempfile.TemporaryDirectory() as d:
            runtime=Runtime(Hass(),Path(d),Latch(FakeStore()),SETTINGS)
            self.assertEqual(len(runtime.jobs),2)
            self.assertEqual(len({id(m.lock) for m in runtime.jobs.values()}),2)
            self.assertTrue(all(not m.allow_pause for m in runtime.jobs.values()))
            started=asyncio.Event()
            async def work(): started.set(); await asyncio.Event().wait()
            task=asyncio.create_task(work()); runtime.tasks.add(task); await started.wait()
            await runtime.stop(None)
            self.assertTrue(task.cancelled())
            self.assertTrue(runtime.stopping)
    async def test_notification_names_and_rate_limits_are_per_printer(self):
        class Bus:
            def __init__(self): self.calls=[]
            async def async_call(self,*args,**kwargs): self.calls.append((args,kwargs))
        class Hass:
            def __init__(self): self.services=Bus(); self.states={}
        with tempfile.TemporaryDirectory() as d:
            h=Hass(); runtime=Runtime(h,Path(d),Latch(FakeStore()),SETTINGS)
            p,a=runtime.jobs.values()
            await p.notify('check'); await p.notify('repeat'); await a.notify('check')
            self.assertEqual(len(h.services.calls),2)
            self.assertTrue(all(call[0][:2] == ('notify','example_phone') for call in h.services.calls))
            titles=[c[0][2]['title'] for c in h.services.calls]
            self.assertEqual(titles,['Printer One print check','Printer Two print check'])

    async def test_diagnostic_classification_and_isolation(self):
        self.assertEqual(diagnostic_outcome('screen_clear_or_unjudgeable'),'screen_success')
        self.assertEqual(diagnostic_outcome('monitor_only'),'comparison_success')
        self.assertEqual(diagnostic_outcome('comparison_clear_or_unjudgeable'),'comparison_success')
        self.assertEqual(diagnostic_outcome('ineligible'),'skipped')
        self.assertEqual(diagnostic_outcome('unavailable_or_error'),'failure')
        self.assertEqual(diagnostic_outcome('unexpected'),'failure')
        with tempfile.TemporaryDirectory() as d:
            runtime=Runtime(SimpleNamespace(),Path(d),Latch(FakeStore()),SETTINGS)
            names=list(runtime.jobs)
            observed=[]
            runtime.observers.append(observed.append)
            async def result(value): return value
            await runtime.run(names[0],SimpleNamespace(run=lambda: result('ineligible')))
            timestamp=runtime.diagnostics[names[0]]['last_check']
            await runtime.run(names[0],SimpleNamespace(run=lambda: result('ineligible')))
            self.assertEqual(observed,[names[0]])
            self.assertEqual(runtime.diagnostics[names[0]]['last_check'],timestamp)
            self.assertEqual(runtime.diagnostics[names[1]]['outcome'],'not_checked')
            await runtime.run(names[0],SimpleNamespace(run=lambda: result('screen_clear_or_unjudgeable')))
            self.assertEqual(runtime.diagnostics[names[0]]['outcome'],'screen_success')
            self.assertEqual(set(runtime.diagnostics[names[0]]),{'outcome','last_check'})
            async def fail(): raise RuntimeError('sensitive details')
            await runtime.run(names[1],SimpleNamespace(run=fail))
            self.assertEqual(runtime.diagnostics[names[1]]['outcome'],'failure')
            self.assertEqual(runtime.diagnostics[names[1]]['failure_phase'],'runtime')
            self.assertEqual(runtime.diagnostics[names[1]]['failure_category'],'other')
            self.assertNotIn('sensitive details',str(runtime.diagnostics))
            await runtime.run(names[1],SimpleNamespace(run=lambda: result('screen_clear_or_unjudgeable')))
            self.assertNotIn('failure_phase',runtime.diagnostics[names[1]])
            self.assertNotIn('failure_category',runtime.diagnostics[names[1]])

    async def test_sensor_publishes_only_its_printer_and_removes_observer(self):
        import importlib
        class SensorEntity:
            async def async_added_to_hass(self): pass
            def async_on_remove(self, callback): self.remove_callback=callback
            def async_write_ha_state(self): self.writes+=1
        modules={name:types.ModuleType(name) for name in ('homeassistant','homeassistant.components','homeassistant.components.sensor','homeassistant.const','homeassistant.core')}
        modules['homeassistant.components.sensor'].SensorEntity=SensorEntity
        modules['homeassistant.const'].EntityCategory=SimpleNamespace(DIAGNOSTIC='diagnostic')
        modules['homeassistant.core'].callback=lambda function: function
        with patch.dict(sys.modules,modules):
            sys.modules.pop('p1s_guard.sensor',None)
            sensor=importlib.import_module('p1s_guard.sensor')
            with tempfile.TemporaryDirectory() as d:
                runtime=Runtime(SimpleNamespace(),Path(d),Latch(FakeStore()),SETTINGS)
                entities=[]
                hass=SimpleNamespace(data={'p1s_guard':runtime})
                await sensor.async_setup_platform(hass,{},entities.extend,{})
                self.assertEqual(len(entities),2)
                first, second=entities
                first.writes=0; second.writes=0
                await first.async_added_to_hass(); await second.async_added_to_hass()
                async def clear(): return 'screen_clear_or_unjudgeable'
                await runtime.run(first.printer.prefix,SimpleNamespace(run=clear))
                self.assertEqual((first.writes,second.writes),(1,0))
                self.assertEqual(first.native_value,'screen_success')
                self.assertEqual(set(first.extra_state_attributes),{'last_check'})
                async def failure(): return 'unavailable_or_error'
                detail={'phase':'frame_validation','category':'invalid_data','reason':'duplicate_frame'}
                with self.assertLogs('p1s_guard',level='WARNING'):
                    await runtime.run(first.printer.prefix,SimpleNamespace(run=failure,last_error=detail))
                self.assertEqual(first.extra_state_attributes['failure_reason'],'duplicate_frame')
                await runtime.run(first.printer.prefix,SimpleNamespace(run=clear))
                self.assertNotIn('failure_reason',first.extra_state_attributes)
                first.remove_callback(); second.remove_callback()
                self.assertEqual(runtime.observers,[])


class ConfigurationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_configured_runtime_only_calls_ai_and_notify_for_each_printer(self):
        from datetime import datetime, timezone
        config=example_config()
        config['ai_task']='ai_task.review_vision'
        config['notify_service']='notify.review_channel'
        config['printers']=[
            {'name':'Alpha','entity_prefix':'alpha','camera':'camera.alpha_view'},
            {'name':'Beta','entity_prefix':'beta','camera':'camera.beta_view'},
        ]
        settings=parse_config(config)
        calls=[]
        class Bus:
            async def async_call(self,domain,service,data,**kwargs):
                calls.append((domain,service,data))
                if (domain,service)==('ai_task','generate_data'):
                    return {'data':{
                        f'frame_{index}_{key}':value
                        for index in range(1,len(data['attachments'])+1)
                        for key,value in [('judgeable',True),('score',0.9),('evidence','synthetic assessment')]
                    }}
                if (domain,service)!=('notify','review_channel'):
                    raise AssertionError('unexpected service')
        states={}
        now=datetime.now(timezone.utc)
        for printer in settings.printers:
            for domain,suffix,value in [('sensor','print_status','running'),('sensor','current_stage','printing'),('binary_sensor','online','on'),('binary_sensor','print_error','off'),('sensor','task_name','example job'),('sensor','start_time','example start')]:
                states[printer.entity(domain,suffix)]=SimpleNamespace(state=value,last_changed=now)
            states[printer.camera]=SimpleNamespace(state='idle',last_changed=now)
        ticks=[0.0]
        captures={printer.prefix:0 for printer in settings.printers}
        async def capture(hass,printer):
            captures[printer.prefix]+=1
            index=captures[printer.prefix]
            # Distinct bytes and test-only trusted IDs must still never arm HA.
            return Capture(synthetic_jpeg(index),ticks[0],f'{printer.prefix}-{index}')
        async def advance(seconds): ticks[0]+=seconds
        async def forbidden(*args): raise AssertionError('pause reservation must remain unused')
        with tempfile.TemporaryDirectory() as directory:
            hass=SimpleNamespace(states=states,services=Bus())
            runtime=Runtime(hass,Path(directory),SimpleNamespace(reserve=forbidden),settings)
            runtime.staging.startup()
            with patch('p1s_guard.camera_capture',capture):
                for name,monitor in runtime.jobs.items():
                    monitor.clock=lambda:ticks[0]
                    monitor.sleep=advance
                    await runtime.run(name,monitor)
                    self.assertEqual(runtime.diagnostics[name]['outcome'],'comparison_success')
                    self.assertFalse(monitor.allow_pause)
            self.assertEqual(captures,{'alpha':3,'beta':3})
            self.assertEqual(list(Path(directory).iterdir()),[])
            self.assertEqual(runtime.staging.active,{})
        ai=[data for domain,service,data in calls if domain=='ai_task']
        notifications=[data for domain,service,data in calls if domain=='notify']
        self.assertEqual(len(calls),6)
        self.assertEqual(len(ai),4)
        self.assertTrue(all(data['entity_id']=='ai_task.review_vision' for data in ai))
        self.assertEqual([data['title'] for data in notifications],['Alpha print check','Beta print check'])
        self.assertTrue(all('No pause was sent' in data['message'] for data in notifications))

    async def test_startup_failure_does_not_log_exception_text_or_paths(self):
        modules={name:types.ModuleType(name) for name in ('homeassistant','homeassistant.const','homeassistant.core','homeassistant.helpers','homeassistant.helpers.event','homeassistant.helpers.storage','homeassistant.helpers.discovery')}
        modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP='stop'
        modules['homeassistant.core'].callback=lambda function: function
        modules['homeassistant.helpers.event'].async_track_time_interval=lambda *args: None
        modules['homeassistant.helpers.storage'].Store=FakeStore
        modules['homeassistant.helpers.discovery'].async_load_platform=lambda *args: None
        def fail(*args): raise PermissionError('synthetic-private-path-and-token')
        with tempfile.TemporaryDirectory() as directory:
            hass=SimpleNamespace(config=SimpleNamespace(media_dirs={'local':directory}),data={})
            with patch.dict(sys.modules,modules), patch('p1s_guard.Staging.startup',fail):
                with self.assertLogs('p1s_guard',level='ERROR') as logs:
                    self.assertFalse(await async_setup(hass,{'p1s_guard':example_config()}))
            self.assertEqual(hass.data,{})
            self.assertIn('permission',str(logs.output))
            self.assertNotIn('synthetic-private',str(logs.output))
            self.assertNotIn('Traceback',str(logs.output))

    async def test_absent_or_invalid_configuration_has_no_startup_side_effects(self):
        self.assertTrue(await async_setup(SimpleNamespace(), {}))
        with self.assertLogs('p1s_guard', level='ERROR') as logs:
            self.assertFalse(await async_setup(SimpleNamespace(), {'p1s_guard':None}))
            config = example_config()
            config['ai_task'] = 'synthetic-private-binding'
            self.assertFalse(await async_setup(SimpleNamespace(), {'p1s_guard':config}))
        self.assertNotIn('synthetic-private-binding', str(logs.output))

    def test_yaml_schema_preserves_other_integrations_and_redacts_errors(self):
        class Invalid(Exception): pass
        module = types.ModuleType('probatio')
        module.Invalid = Invalid
        with patch.dict(sys.modules, {'probatio':module}):
            config = {'p1s_guard':example_config(), 'unrelated':{'value':'keep'}}
            self.assertIs(CONFIG_SCHEMA(config), config)
            self.assertEqual(config['unrelated'], {'value':'keep'})
            self.assertEqual(CONFIG_SCHEMA({'unrelated':{}}), {'unrelated':{}})
            config['p1s_guard']['ai_task'] = 'synthetic-private-binding'
            with self.assertRaises(Invalid) as result:
                CONFIG_SCHEMA(config)
            self.assertNotIn('synthetic-private-binding', str(result.exception))

    async def test_configured_ai_and_camera_routing_remains_monitor_only(self):
        class Bus:
            def __init__(self): self.calls = []
            async def async_call(self, domain, service, data, **kwargs):
                self.calls.append((domain, service, data))
                return {'data':{'frame_1_judgeable':False,'frame_1_score':0,'frame_1_evidence':''}}
        captured = []
        async def capture(hass, printer):
            captured.append(printer.camera)
            return SimpleNamespace(content=b'1234')
        with tempfile.TemporaryDirectory() as directory:
            hass = SimpleNamespace(services=Bus(),states={})
            runtime = Runtime(hass,Path(directory),Latch(FakeStore()),SETTINGS)
            runtime.staging.startup()
            with patch('p1s_guard.camera_capture',capture):
                for monitor in runtime.jobs.values():
                    frame = await monitor.capture()
                    await monitor.screen(frame,{})
                    with self.assertRaisesRegex(RuntimeError,'automatic pause is disabled'):
                        await monitor.pause()
                    self.assertFalse(monitor.allow_pause)
            self.assertEqual(captured,[printer.camera for printer in SETTINGS.printers])
            self.assertTrue(all(call[:2] == ('ai_task','generate_data') for call in hass.services.calls))
            self.assertTrue(all(call[2]['entity_id'] == SETTINGS.ai_task for call in hass.services.calls))
            self.assertEqual(len(hass.services.calls),2)
            self.assertEqual(list(Path(directory).iterdir()),[])

    async def test_failure_reason_is_allowlisted_and_cleared_after_success(self):
        async def failure(): return 'unavailable_or_error'
        async def success(): return 'screen_clear_or_unjudgeable'
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(SimpleNamespace(),Path(directory),Latch(FakeStore()),SETTINGS)
            name = next(iter(runtime.jobs))
            detail = {'phase':'frame_validation','category':'invalid_data','reason':'duplicate_frame'}
            with self.assertLogs('p1s_guard', level='WARNING') as logs:
                await runtime.run(name,SimpleNamespace(run=failure,last_error=detail))
            self.assertEqual(runtime.diagnostics[name]['failure_reason'],'duplicate_frame')
            self.assertNotIn(name,str(logs.output))
            await runtime.run(name,SimpleNamespace(run=success))
            self.assertNotIn('failure_reason',runtime.diagnostics[name])
            detail['reason'] = 'synthetic-private-data'
            with self.assertLogs('p1s_guard', level='WARNING'):
                await runtime.run(name,SimpleNamespace(run=failure,last_error=detail))
            self.assertNotIn('failure_reason',runtime.diagnostics[name])
            self.assertNotIn('synthetic-private-data',str(runtime.diagnostics))
