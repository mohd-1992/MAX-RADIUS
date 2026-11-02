"""Pure audit presentation: no app import, database or credentials."""
import datetime
import importlib.util
import json
from pathlib import Path
import unittest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('subscriber_audit_fixture', ROOT/'services/subscriber_audit_service.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class SubscriberAuditTests(unittest.TestCase):
    def log(self, changes):
        return {'id': 1, 'action': 'EDIT_CONTRACT', 'change_details': json.dumps(changes)}

    def test_only_changed_fields_and_password_marker(self):
        current = dict(username='fixture', service_type='hotspot', expires_at=datetime.datetime(2026, 10, 24),
                       password='old-secret', full_name='Before')
        desired = dict(username='fixture', service_type='hotspot', expires_at=datetime.datetime(2026, 10, 24),
                       personal=dict(password='new-secret', full_name='After'))
        changes = audit.subscriber_changes(current, desired)
        self.assertEqual(set(changes), {'password', 'full_name'})
        self.assertEqual(changes['password'], {'changed': True})
        self.assertNotIn('secret', json.dumps(changes))

    def test_empty_and_equal_values_do_not_create_changes(self):
        desired = dict(username='fixture', service_type='hotspot', expires_at=None, personal={})
        self.assertEqual(audit.subscriber_changes(dict(username='fixture', service_type='hotspot'), desired), {})

    def test_old_json_shows_changed_expiry_only(self):
        log = self.log({'username': {'before': 'fixture', 'after': 'fixture'},
                        'expires_at': {'before': '2026-10-24 01:00:00', 'after': '2026-11-24 01:00:00'}})
        shown = audit.present_subscriber_audit([log])[0]
        self.assertEqual(shown['action_label'], 'تعديل بيانات المشترك')
        self.assertEqual(len(shown['detail_lines']), 1)
        self.assertIn('تاريخ الانتهاء', shown['detail_lines'][0])
        self.assertNotIn('username', shown['detail_lines'][0])
        self.assertEqual(json.loads(log['change_details'])['username']['before'], 'fixture')

    def test_legacy_password_values_and_unknown_fields_not_displayed(self):
        shown = audit.present_subscriber_audit([self.log({'password': {'before': 'old-secret', 'after': 'new-secret'},
                                                         'private_token': {'before': '', 'after': 'secret'}})])[0]
        self.assertEqual(shown['detail_lines'], ['تم تغيير كلمة المرور'])
        self.assertNotIn('secret', str(shown['detail_lines']))

    def test_legacy_null_and_empty_bindings_are_not_false_changes(self):
        changes = {'mac_binding': {'before': None, 'after': ''},
                   'static_ip': {'before': '', 'after': None},
                   'full_name': {'before': 'Before', 'after': 'After'}}
        shown = audit.present_subscriber_audit([self.log(changes)])[0]
        self.assertEqual(shown['detail_lines'], ['الاسم الكامل: من «Before» إلى «After»'])

    def test_retains_old_rows_and_plain_text_entries(self):
        logs = [self.log({}), dict(id=2, action='UPDATE_PROFILE', change_details='تم تعديل الهاتف')]
        shown = audit.present_subscriber_audit(logs)
        self.assertEqual([x['id'] for x in shown], [1, 2])
        self.assertEqual(shown[1]['detail_lines'], ['تم تعديل الهاتف'])
        self.assertEqual(shown[1]['action_label'], 'تحديث البيانات الشخصية')

    def test_malformed_structured_entry_does_not_leak_raw_content(self):
        log = self.log({}); log['change_details'] = '{malformed secret'
        self.assertNotIn('secret', str(audit.present_subscriber_audit([log])[0]['detail_lines']))

    def test_actual_audit_row_escapes_details_and_admin_name(self):
        template = (ROOT/'web/templates/subscriber_details.html').read_text(encoding='utf-8')
        start = template.index('{% for log in audit_logs %}')
        end = template.index('{% endfor %}', template.index('{% endfor %}', start) + len('{% endfor %}'))
        fragment = template[start:end+len('{% endfor %}')]
        log = self.log({'full_name': {'before': 'Before', 'after': '<script>unsafe</script>'}})
        log.update(created_at='2026-10-24', admin_name='<b>admin</b>')
        output = Environment(autoescape=True).from_string(fragment).render(audit_logs=audit.present_subscriber_audit([log]))
        self.assertNotIn('<script>', output)
        self.assertNotIn('<b>admin</b>', output)
        self.assertIn('&lt;script&gt;', output)
        self.assertIn('الاسم الكامل', output)


if __name__ == '__main__':
    unittest.main()
