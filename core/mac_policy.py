"""Pure MAC allow-list validation shared by UI services and offline imports."""
import re

MAX_ADDRESSES = 64

def normalize_mac(value):
    value = str(value or '').strip().upper().replace('-', ':')
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', value):
        raise ValueError('أدخل عنوان MAC صحيحًا مثل AA:BB:CC:DD:EE:FF.')
    return value

def validate_policy(data):
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        raise ValueError('حدد حالة تقييد الماك.')
    rows = data.get('addresses')
    if not isinstance(rows, list) or len(rows) > MAX_ADDRESSES:
        raise ValueError(f'الحد الأقصى {MAX_ADDRESSES} عنوان MAC لكل حساب.')
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('قائمة الماكات غير صالحة.')
        mac = normalize_mac(row.get('mac'))
        note = row.get('note', '')
        if not isinstance(note, str) or len(note.strip()) > 250:
            raise ValueError('ملاحظة الماك بحد أقصى 250 حرفًا.')
        if mac in seen:
            raise ValueError('لا يمكن تكرار الماك داخل القائمة.')
        seen.add(mac)
        clean.append({'mac': mac, 'note': note.strip()})
    if enabled and not clean:
        raise ValueError('أضف ماكًا واحدًا على الأقل قبل تفعيل التقييد.')
    return {'enabled': enabled, 'addresses': sorted(clean, key=lambda row: row['mac'])}
