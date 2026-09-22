# -*- coding: utf-8 -*-
"""
Jinja Filters and UI Utility Helpers for MAX RADIUS Web Application.
"""

from services.user_portal_service import format_mb_or_gb

def jinja_format_mb(val_mb):
    return format_mb_or_gb(val_mb)

def jinja_format_speed(speed):
    if speed is None:
        return 'غير محدود'
    s = str(speed).strip().lower()
    if s in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-'):
        return 'غير محدود'
    return str(speed)

def jinja_format_speed_pair(down, up=None):
    if isinstance(down, (tuple, list)):
        if len(down) >= 2:
            up = down[1]
            down = down[0]
        elif len(down) == 1:
            down = down[0]
            up = None
    elif up is None and isinstance(down, dict):
        up = down.get('rate_upload')
        down = down.get('rate_download')
    
    d_is_unlimited = (down is None) or str(down).strip().lower() in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-')
    u_is_unlimited = (up is None) or str(up).strip().lower() in ('0', '0m', '0k', '0g', '0mb', '0kb', '0gb', 'unlimited', 'none', '', '-')
    
    if d_is_unlimited and u_is_unlimited:
        return 'سرعة مفتوحة'
    if d_is_unlimited:
        return f"⬇️ سرعة مفتوحة / ⬆️ {up}"
    if u_is_unlimited:
        return f"⬇️ {down} / ⬆️ سرعة مفتوحة"
    return f"⬇️ {down} / ⬆️ {up}"

def register_template_filters(app):
    """Registers custom template filters on the Flask application instance."""
    app.template_filter('format_mb')(jinja_format_mb)
    app.template_filter('format_speed')(jinja_format_speed)
    app.template_filter('format_speed_pair')(jinja_format_speed_pair)
