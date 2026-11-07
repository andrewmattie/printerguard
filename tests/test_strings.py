from pathlib import Path
import json
import unittest
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components'))


INTEGRATION = Path(__file__).parents[1] / 'custom_components/printer_guard'


class StringsTests(unittest.TestCase):
    def test_english_translation_matches_source(self):
        source = json.loads((INTEGRATION / 'strings.json').read_text())
        translated = json.loads((INTEGRATION / 'translations/en.json').read_text())
        self.assertEqual(translated, source)

    def test_setup_copy_uses_printer_language(self):
        strings = json.loads((INTEGRATION / 'strings.json').read_text())
        self.assertEqual(strings['config']['step']['user']['title'], 'Add a printer')
        self.assertEqual(strings['options']['step']['init']['title'], 'Configure printer')
        for section in ('config', 'options'):
            with self.subTest(section=section):
                text = json.dumps(strings[section])
                self.assertNotRegex(text, r'\b(?:YAML|UI)\b')
                self.assertNotIn('entity_prefix', text)
                self.assertNotIn('prefix', text)

    def test_direct_entities_have_labels_and_descriptions_in_both_forms(self):
        from printer_guard.config import REQUIRED_ENTITIES, OPTIONAL_ENTITIES
        strings = json.loads((INTEGRATION / 'strings.json').read_text())
        for section, step in (('config', 'user'), ('options', 'init')):
            form = strings[section]['step'][step]
            for field in REQUIRED_ENTITIES | OPTIONAL_ENTITIES:
                with self.subTest(section=section, field=field):
                    self.assertIn(field, form['data'])
                    self.assertIn(field, form['data_description'])
                    self.assertTrue(form['data_description'][field].startswith('Choose '))
                    if field in OPTIONAL_ENTITIES:
                        self.assertIn('leave this empty', form['data_description'][field])

    def test_existing_field_descriptions_remain_plain_and_consistent(self):
        strings = json.loads((INTEGRATION / 'strings.json').read_text())
        setup = strings['config']['step']['user']['data_description']
        options = strings['options']['step']['init']['data_description']
        for field in ('name', 'camera', 'ai_task', 'screen_ai_task', 'notify_service'):
            with self.subTest(field=field):
                self.assertEqual(setup[field], options[field])
        self.assertIn('may charge for requests', setup['ai_task'])
        self.assertIn("won't fall back", setup['screen_ai_task'])
        self.assertIn("won't send a test alert", setup['notify_service'])


if __name__ == '__main__':
    unittest.main()
