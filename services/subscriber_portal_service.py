# -*- coding: utf-8 -*-
"""
MAX RADIUS - Subscriber Portal Management Service.
Handles settings and administration for the Subscriber Self-Service Portal.
"""

import logging
from database.db import query_all, query_one, execute_write

logger = logging.getLogger('subscriber_portal')

ALLOWED_PORTAL_SETTINGS = {
    'enable_user_portal',
    'portal_allow_registration',
    'portal_allow_package_change',
    'portal_allow_password_change',
    'portal_login_username_only',
    'allow_data_loan',
    'loan_amount_mb',
    'loan_threshold_mb',
    'portal_whatsapp',
    'portal_support_phone'
}


def get_subscriber_portal_settings():
    """
    Fetches the configured subscriber portal settings from wisp_system_settings.
    """
    try:
        rows = query_all("SELECT `key`, `value` FROM wisp_system_settings")
        s = {r['key']: r['value'] for r in rows} if rows else {}
    except Exception as e:
        logger.warning("Error fetching subscriber portal settings: %s", e)
        s = {}

    defaults = {
        'enable_user_portal': '1',
        'portal_allow_registration': '1',
        'portal_allow_package_change': '1',
        'portal_allow_password_change': '1',
        'portal_login_username_only': '0',
        'allow_data_loan': '1',
        'loan_amount_mb': '1024',
        'loan_threshold_mb': '100',
        'portal_whatsapp': s.get('support_phone', '967773570053'),
        'portal_support_phone': s.get('support_phone', '773570053')
    }

    result = dict(defaults)
    result.update(s)
    return result


def save_subscriber_portal_settings(data):
    """
    Saves subscriber portal settings to wisp_system_settings.
    """
    for input_key, val in data.items():
        if input_key not in ALLOWED_PORTAL_SETTINGS:
            continue
        v = str(val).strip() if val is not None else ''

        existing = query_one("SELECT `key` FROM wisp_system_settings WHERE `key` = ?", (input_key,))
        if existing:
            execute_write("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?", (v, input_key))
        else:
            execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (?, ?)", (input_key, v))

    return True
