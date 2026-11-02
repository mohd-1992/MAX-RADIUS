"""Readable subscriber audit entries; never expose credential values."""
import datetime
import json


FIELD_LABELS = {
    'username': 'اسم المستخدم', 'service_type': 'نوع الخدمة',
    'expires_at': 'تاريخ الانتهاء', 'mac_binding': 'ربط MAC', 'bound_mac': 'ربط MAC',
    'static_ip': 'عنوان IP', 'full_name': 'الاسم الكامل',
    'phone': 'رقم الهاتف', 'national_id': 'رقم الهوية',
    'address': 'العنوان', 'notes': 'الملاحظات', 'password': 'كلمة المرور',
}
ACTION_LABELS = {'EDIT_CONTRACT': 'تعديل بيانات المشترك',
                 'EDIT_MAC_POLICY': 'تعديل الماكات المسموحة',
                 'EDIT_CARD': 'تعديل بيانات الكرت',
                 'UPDATE_PROFILE': 'تحديث البيانات الشخصية'}


def _value(key, value):
    if key == 'expires_at' and value:
        return datetime.datetime.fromisoformat(str(value)).isoformat(sep=' ', timespec='seconds')
    return '' if value is None else str(value)


def subscriber_changes(current, desired):
    """Only changed fields, with a marker instead of either password."""
    values = dict(desired['personal'], username=desired['username'],
                  service_type=desired['service_type'], expires_at=desired['expires_at'])
    changes = {}
    for key in FIELD_LABELS:
        if key == 'bound_mac':
            continue
        before, after = _value(key, current.get(key)), _value(key, values.get(key))
        if before != after:
            changes[key] = {'changed': True} if key == 'password' else {'before': before, 'after': after}
    return changes


def _display(key, value):
    if value in (None, ''):
        return {'expires_at': 'بلا تاريخ انتهاء', 'mac_binding': 'بلا ربط', 'bound_mac': 'بلا ربط',
                'static_ip': 'تلقائي'}.get(key, 'غير محدد')
    if key == 'service_type':
        return {'hotspot': 'Hotspot', 'pppoe': 'PPPoE'}.get(str(value), str(value))
    return str(value)


def present_subscriber_audit(logs):
    """Format old structured entries at read time; retain every historical row."""
    result = []
    for original in logs:
        log = dict(original)
        log['action_label'] = ACTION_LABELS.get(log['action'], log['action'])
        details = log.get('change_details') or ''
        if log['action'] == 'EDIT_MAC_POLICY':
            try:
                change = json.loads(details)['mac_policy']
                lines = []
                for key, label in (('before', 'قبل'), ('after', 'بعد')):
                    state = change[key]
                    mode = 'مفعّل' if state['enabled'] else 'غير مقيّد'
                    lines.append(f"{label}: {mode}، {len(state['addresses'])} عنوان مسجل")
                    lines.extend(f"{item['mac']} — {item['note']}" if item['note'] else item['mac'] for item in state['addresses'])
                log['detail_lines'] = lines
            except (ValueError, TypeError, KeyError):
                log['detail_lines'] = ['تعديل قائمة الماكات المسموحة.']
        elif log['action'] in ('EDIT_CONTRACT', 'EDIT_CARD'):
            try:
                changes = json.loads(details)
                if not isinstance(changes, dict):
                    raise ValueError('Invalid audit entry')
            except (ValueError, TypeError):
                changes = {}
            lines = []
            for key, label in FIELD_LABELS.items():
                change = changes.get(key)
                if not isinstance(change, dict):
                    continue
                if key == 'password':
                    if change.get('changed') or change.get('before') != change.get('after'):
                        lines.append('تم تغيير كلمة المرور')
                elif _display(key, change.get('before')) != _display(key, change.get('after')):
                    lines.append(f"{label}: من «{_display(key, change.get('before'))}» إلى «{_display(key, change.get('after'))}»")
            log['detail_lines'] = lines or ['تحديث بيانات المشترك دون تغييرات موضحة في حقول العقد المسجلة.']
        else:
            log['detail_lines'] = [details]
        result.append(log)
    return result
