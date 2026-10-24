"""Atomic settings saves; verified branding entitlement precedes every effect."""
import json
import os
from pathlib import Path
import tempfile
import uuid

from core.license_feature_policy import BRANDING_FIELDS, FeatureUnavailable, enabled


class SettingsSaveUncertain(RuntimeError):
    pass


def _database():
    from database.db import get_connection, adapt_query
    return get_connection, adapt_query


def setting_values(form):
    currency = form.get('currency', 'YER').strip()
    symbols = {'YER': 'ر.ي', 'EGP': 'ج.م', 'IQD': 'د.ع', 'SYP': 'ل.س',
               'SAR': 'ر.س', 'AED': 'د.إ', 'USD': '$'}
    name = form.get('network_name', '').strip() or form.get('company_name', '').strip() or 'MAX RADIUS'
    values = {
        'network_name': name, 'company_name': name, 'isp_name': name,
        'system_title': form.get('system_title', 'MAX RADIUS - نظام إدارة الشبكات والفوترة و FreeRADIUS').strip(),
        'currency': currency,
        'currency_symbol': form.get('currency_symbol', '').strip() or symbols.get(currency, 'ر.ي'),
        'timezone': form.get('timezone', 'Asia/Aden').strip(),
        'support_phone': form.get('support_phone', '').strip(),
        'support_email': form.get('support_email', '').strip(),
        'address': form.get('address', '').strip(),
        'hotspot_domain': form.get('hotspot_domain', 'wifi.hotspot').strip(),
        'default_coa_port': form.get('default_coa_port', '3799').strip(),
        'sms_gateway_url': form.get('sms_gateway_url', '').strip(),
        'sms_api_key': form.get('sms_api_key', '').strip(),
        'sms_sender_id': form.get('sms_sender_id', 'WISP-NET').strip(),
        'portal_whatsapp': form.get('portal_whatsapp', '').strip(),
        'portal_support_phone': form.get('portal_support_phone', '').strip(),
    }
    for key in ('portal_allow_registration', 'portal_allow_package_change',
                'portal_allow_password_change', 'portal_login_username_only', 'allow_data_loan'):
        values[key] = '1' if form.get(key) in ('1', 'on', 'true', True, 1) else '0'
    for key, minimum, default in (('loan_amount_mb', 10, 1024), ('loan_threshold_mb', 1, 100)):
        raw = form.get(key, '').strip()
        values[key] = str(max(minimum, int(raw))) if raw.isdigit() else str(default)
    return values


def save_system_settings(form, logo, static_folder):
    # The active signed license is authoritative, including legacy feature defaults.
    branding = enabled('white_label')
    uploading = bool(logo and logo.filename)
    if not branding and (BRANDING_FIELDS.intersection(form) or uploading):
        raise FeatureUnavailable('white_label')
    values = setting_values(form)
    if not branding:
        values = {k: v for k, v in values.items() if k not in BRANDING_FIELDS}
    extension = Path(logo.filename).suffix.lower() if uploading else ''
    if uploading and extension not in ('.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico'):
        raise ValueError('صيغة الشعار غير مدعومة.')

    factory, adapt = _database()
    temporary = destination = connection = cursor = None
    commit_started = False
    token = uuid.uuid4().hex
    try:
        if uploading:
            uploads = Path(static_folder) / 'uploads'
            uploads.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix='.settings-logo-', dir=uploads)
            os.close(handle)
            logo.save(temporary)
            destination = uploads / ('logo_' + token + extension)
            values['network_logo'] = destination.name

        connection = factory()
        sqlite = connection.__class__.__module__.startswith('sqlite3')
        if sqlite:
            connection.execute('BEGIN IMMEDIATE')
        cursor = connection.cursor()
        cursor.execute('SELECT `key`, `value` FROM wisp_system_settings' + ('' if sqlite else ' FOR UPDATE'))
        rows = cursor.fetchall()
        existing = {row['key'] if hasattr(row, 'keys') else row[0] for row in rows}
        # A unique new file is installed before commit. The old logo is never touched.
        if uploading:
            os.replace(temporary, destination)
            temporary = None
        for key, value in values.items():
            if key in existing:
                sql, params = 'UPDATE wisp_system_settings SET `value`=? WHERE `key`=?', (value, key)
            else:
                sql, params = 'INSERT INTO wisp_system_settings (`key`,`value`,`description`) VALUES (?,?,?)', (key, value, 'إعداد نظام')
            cursor.execute(adapt(sql, connection), params)
        details = json.dumps({'operation': token, 'name': values.get('network_name'),
                              'currency': values['currency'], 'timezone': values['timezone']}, ensure_ascii=False)
        cursor.execute(adapt('INSERT INTO wisp_audit_logs (admin_id,username,action,module,details,ip_address) VALUES (?,?,?,?,?,?)', connection),
                       (1, 'admin', 'UPDATE_SETTINGS', 'settings', details, '127.0.0.1'))
        commit_started = True
        connection.commit()
        return values
    except Exception as error:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        if commit_started:
            # A lost commit reply is not proof of rollback. Retain the new file so
            # a possibly committed logo reference remains valid; never auto-replay.
            raise SettingsSaveUncertain('تعذر تأكيد نتيجة حفظ الإعدادات؛ أعد قراءة القيم قبل المحاولة مجددًا.') from error
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
