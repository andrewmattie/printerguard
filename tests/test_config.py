from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components'))
from printer_guard.config import parse_config
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
            self.assertEqual(settings.printers[0].task_name, 'sensor.printer_one_task_name')

    def test_optional_screen_task_is_explicit_and_preserves_comparison_task(self):
        config = example_config()
        self.assertIsNone(parse_config(config).screen_ai_task)
        config['screen_ai_task'] = 'ai_task.example_screen'
        settings = parse_config(config)
        self.assertEqual(settings.screen_ai_task, 'ai_task.example_screen')
        self.assertEqual(settings.ai_task, config['ai_task'])
        for value in ('', None, 42, 'camera.example', 'ai_task.', 'ai_task.bad/name'):
            config['screen_ai_task'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_config(config)

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
        printers = example_config()['printers']
        printers.append({'name': 'Printer Three', 'entity_prefix': 'printer_three', 'camera': 'camera.printer_three'})
        for key, values in {
            'ai_task': ('', None, 'camera.example', 'ai_task.', 'ai_task.bad/name'),
            'notify_service': ('', None, 'button.example', 'notify.', 'notify.bad name'),
            'printers': ([], 'printer_one', {}, printers),
        }.items():
            for value in values:
                config = example_config()
                config[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    parse_config(config)

    def test_printer_fields_and_unknown_controls_are_rejected(self):
        for key, values in {
            'name': ('', '   ', None, 'line\nbreak', 'x' * 81),
            'print_status': ('', None, 'button.example', 'with space'),
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

    def test_duplicate_printer_bindings_are_rejected(self):
        for key in example_config()['printers'][0].keys() - {'name'}:
            config = example_config()
            config['printers'][1][key] = config['printers'][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_config(config)

    def test_each_required_binding_and_all_entity_domains_are_validated(self):
        from printer_guard.config import REQUIRED_ENTITIES, OPTIONAL_ENTITIES
        for key in REQUIRED_ENTITIES:
            config = example_config()
            del config['printers'][0][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                parse_config(config)
        for key in REQUIRED_ENTITIES | OPTIONAL_ENTITIES:
            for value in (None, '', 'button.wrong', 42, 'sensor.private/bad'):
                config = example_config()
                config['printers'][0][key] = value
                with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                    parse_config(config)

    def test_arbitrary_entities_and_omitted_telemetry(self):
        from printer_guard.config import OPTIONAL_ENTITIES
        config = example_config()
        printer = config['printers'][0]
        printer['print_status'] = 'sensor.custom_printer_activity'
        printer['online'] = 'binary_sensor.connection_state'
        for key in OPTIONAL_ENTITIES:
            del printer[key]
        parsed = parse_config(config).printers[0]
        self.assertEqual(parsed.print_status, printer['print_status'])
        self.assertEqual(parsed.online, printer['online'])
        self.assertEqual(parsed.key, 'sensor.custom_printer_activity')
        self.assertTrue(all(getattr(parsed, key) is None for key in OPTIONAL_ENTITIES))
        self.assertEqual(len(parsed.bindings), 7)

    def test_legacy_yaml_expands_without_changing_identity(self):
        from printer_guard.config import REQUIRED_ENTITIES, OPTIONAL_ENTITIES
        config = example_config()
        expected = parse_config(config)
        config['printers'] = [
            {'name': printer.name, 'entity_prefix': printer.key, 'camera': printer.camera}
            for printer in expected.printers]
        self.assertEqual(parse_config(config), expected)
        for prefix in ('', None, 'sensor.example', 'with space'):
            config['printers'][0]['entity_prefix'] = prefix
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                parse_config(config)
        config['printers'][0]['entity_prefix'] = 'printer_one'
        config['printers'][0]['print_status'] = 'sensor.explicit'
        with self.assertRaises(ValueError):
            parse_config(config)

    def test_reused_roles_and_identity_collisions_are_rejected(self):
        config = example_config()
        config['printers'][0]['task_name'] = config['printers'][0]['print_status']
        with self.assertRaises(ValueError):
            parse_config(config)
        config = example_config()
        config['printers'][0]['print_status'] = 'sensor.alpha'
        config['printers'][1]['print_status'] = 'sensor.alpha_print_status'
        self.assertEqual([printer.key for printer in parse_config(config).printers], ['sensor.alpha', 'alpha'])
        config['printers'][0]['print_status'] = 'sensor._print_status'
        self.assertEqual(parse_config(config).printers[0].key, 'sensor._print_status')

    def test_saved_ui_identity_survives_binding_edits(self):
        config = example_config()
        config['printers'] = config['printers'][:1]
        config['printers'][0]['print_status'] = 'sensor.replacement_status'
        self.assertEqual(parse_config(config, printer_id='printer_one').printers[0].key, 'printer_one')

    def test_errors_do_not_echo_private_values(self):
        private_value = 'synthetic-private-value'
        for key in ('ai_task', 'screen_ai_task'):
            config = example_config()
            config[key] = private_value
            with self.subTest(key=key), self.assertRaises(ValueError) as result:
                parse_config(config)
            self.assertNotIn(private_value, str(result.exception))


if __name__ == '__main__':
    unittest.main()
