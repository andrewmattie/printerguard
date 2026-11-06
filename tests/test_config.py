from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components'))
from p1s_guard.config import parse_config
from guard_fixtures import example_config


class ConfigTests(unittest.TestCase):
    def test_explicit_bindings_are_preserved_for_one_or_two_printers(self):
        for count in (1, 2):
            config = example_config()
            config['printers'] = config['printers'][:count]
            settings = parse_config(config)
            self.assertEqual(settings.ai_task, config['ai_task'])
            self.assertEqual(settings.notify_service, config['notify_service'])
            self.assertEqual(len(settings.printers), count)
            self.assertEqual(settings.printers[0].entity('sensor', 'task_name'), 'sensor.printer_one_task_name')

    def test_missing_unknown_and_wrongly_typed_settings_are_rejected(self):
        for value in (None, {}, [], True, {'printers': []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_config(value)
        for key in example_config():
            config = example_config()
            del config[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                parse_config(config)
        config = example_config()
        config['allow_pause'] = True
        with self.assertRaises(ValueError):
            parse_config(config)

    def test_entity_domains_and_printer_count_are_validated(self):
        for key, values in {
            'ai_task': ('', None, 'camera.example', 'ai_task.', 'ai_task.bad/name'),
            'notify_service': ('', None, 'button.example', 'notify.', 'notify.bad name'),
            'printers': ([], 'printer_one', {}, example_config()['printers'] * 2),
        }.items():
            for value in values:
                config = example_config()
                config[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    parse_config(config)

    def test_printer_fields_and_unknown_controls_are_rejected(self):
        for key, values in {
            'name': ('', '   ', None, 'line\nbreak', 'x' * 81),
            'entity_prefix': ('', None, 'sensor.example', 'with space'),
            'camera': ('camera.', 'button.example', None),
        }.items():
            for value in values:
                config = example_config()
                config['printers'][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    parse_config(config)
        for entry in (None, {}, {'name': 'Example'}, {**example_config()['printers'][0], 'pause': 'button.example'}):
            config = example_config()
            config['printers'][0] = entry
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                parse_config(config)

    def test_duplicate_printer_prefix_or_camera_is_rejected(self):
        for key in ('entity_prefix', 'camera'):
            config = example_config()
            config['printers'][1][key] = config['printers'][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_config(config)

    def test_errors_do_not_echo_private_values(self):
        private_value = 'synthetic-private-value'
        config = example_config()
        config['ai_task'] = private_value
        with self.assertRaises(ValueError) as result:
            parse_config(config)
        self.assertNotIn(private_value, str(result.exception))


if __name__ == '__main__':
    unittest.main()
