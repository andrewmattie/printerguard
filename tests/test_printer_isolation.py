import importlib
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import types
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components'))
runtime=importlib.import_module('p1s_guard.ha_runtime')
from guard_fixtures import SETTINGS

class IsolationTests(unittest.TestCase):
    def test_configured_camera_and_state_bindings_are_separate(self):
        p,a=SETTINGS.printers
        self.assertEqual(p.camera,'camera.printer_one')
        self.assertEqual(a.camera,'camera.printer_two')
        self.assertNotEqual(p.camera,a.camera)
        self.assertNotEqual(p.entity('sensor','print_status'),a.entity('sensor','print_status'))
    def test_context_reads_only_selected_printer(self):
        p,a=SETTINGS.printers
        now=datetime.now(timezone.utc)
        state=lambda v: SimpleNamespace(state=v,last_changed=now)
        states={}
        for printer,job in ((p,'example job one'),(a,'example job two')):
            for domain,suffix,value in [('sensor','print_status','running'),('sensor','current_stage','printing'),('binary_sensor','online','on'),('binary_sensor','print_error','off'),('sensor','task_name',job),('sensor','start_time','time')]:
                states[printer.entity(domain,suffix)]=state(value)
            states[printer.camera]=state('idle')
        self.assertEqual(runtime.context_from_states(states,p).task,'example job one')
        self.assertEqual(runtime.context_from_states(states,a).task,'example job two')
        states[a.entity('binary_sensor','online')]=state('off')
        self.assertTrue(runtime.context_from_states(states,p).eligible)
        self.assertFalse(runtime.context_from_states(states,a).eligible)

class CameraTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_camera_format_is_bounded_and_does_not_expose_metadata(self):
        module = types.ModuleType('homeassistant.components.camera')
        async def image(hass, camera):
            return SimpleNamespace(content_type='synthetic-private-content-type',content=b'synthetic-private-data')
        module.async_get_image = image
        with patch.dict(sys.modules,{'homeassistant.components.camera':module}):
            with self.assertRaises(runtime.FrameValidationError) as result:
                await runtime.camera_capture(None,SETTINGS.printers[0])
        self.assertEqual(result.exception.reason,'unsupported_image_format')
        self.assertNotIn('synthetic-private',str(result.exception))


if __name__=='__main__': unittest.main()
