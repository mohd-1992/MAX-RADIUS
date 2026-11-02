"""Pure allow-list contract; no application, database, Docker or NAS imports."""
import ast
import re
import unittest
from pathlib import Path

ns = {'re': re, 'MAX_ADDRESSES': 64}
tree = ast.parse(Path('core/mac_policy.py').read_text(encoding='utf-8'))
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('normalize_mac', 'validate_policy')], type_ignores=[]), '<mac-policy-contract>', 'exec'), ns)
validate = ns['validate_policy']


class MacPolicyContractTests(unittest.TestCase):
    def test_unrestricted_default_explicit_empty(self):
        self.assertEqual(validate({'enabled': False, 'addresses': []}), {'enabled': False, 'addresses': []})

    def test_seven_macs_do_not_define_session_limit(self):
        rows = [{'mac': f'02:00:00:00:00:{i:02X}', 'note': f'جهاز {i}'} for i in range(7)]
        policy = validate({'enabled': True, 'addresses': rows, 'simultaneous_sessions': 999})
        self.assertEqual(len(policy['addresses']), 7)
        self.assertNotIn('simultaneous_sessions', policy)

    def test_normalizes_case_separator_whitespace_and_notes(self):
        result = validate({'enabled': True, 'addresses': [{'mac': ' aa-bb-cc-dd-ee-ff ', 'note': ' هاتف '}]})
        self.assertEqual(result['addresses'], [{'mac': 'AA:BB:CC:DD:EE:FF', 'note': 'هاتف'}])

    def test_duplicate_after_normalization_rejected(self):
        with self.assertRaises(ValueError):
            validate({'enabled': True, 'addresses': [{'mac': 'aa-bb-cc-dd-ee-ff'}, {'mac': 'AA:BB:CC:DD:EE:FF'}]})

    def test_enabled_empty_fails_closed(self):
        with self.assertRaises(ValueError):
            validate({'enabled': True, 'addresses': []})

    def test_disabled_can_keep_devices_for_later(self):
        self.assertEqual(len(validate({'enabled': False, 'addresses': [{'mac': '02:00:00:00:00:01'}]})['addresses']), 1)

    def test_invalid_mac_payloads_rejected(self):
        for value in ('', 'AA:BB:CC:DD:EE', 'GG:BB:CC:DD:EE:FF', "' OR 1=1 --", ['AA:BB:CC:DD:EE:FF']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({'enabled': True, 'addresses': [{'mac': value}]})

    def test_bool_and_collection_types_are_strict(self):
        for data in ({'enabled': 'false', 'addresses': []}, {'enabled': 1, 'addresses': []}, {'enabled': False, 'addresses': {}}, {'enabled': False, 'addresses': [None]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate(data)

    def test_list_and_note_bounds(self):
        for rows in ([{'mac': '02:00:00:00:00:01', 'note': 'أ' * 251}], [{'mac': '02:00:00:00:00:01', 'note': {}}], [{}] * 65):
            with self.assertRaises(ValueError):
                validate({'enabled': False, 'addresses': rows})

    def test_notes_are_plain_text_not_html(self):
        result = validate({'enabled': True, 'addresses': [{'mac': '02:00:00:00:00:01', 'note': '<img src=x onerror=alert(1)>'}]})
        self.assertEqual(result['addresses'][0]['note'], '<img src=x onerror=alert(1)>')
        # The UI sets input.value/textContent; it must never interpret a note as HTML.


if __name__ == '__main__':
    unittest.main()
