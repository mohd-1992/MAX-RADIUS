"""Shared lifecycle and canonical accounting values for file/API imports."""
import datetime
import uuid


def present_value(mapping, key, fallback):
    value = mapping.get(key)
    return fallback if value is None or value == '' else value


def parse_import_datetime(value):
    if value is None or str(value).strip().lower() in ('', 'none', 'never', 'unlimited'):
        return None
    if isinstance(value, datetime.datetime):
        result = value
    else:
        text = str(value).strip()
        try:
            result = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            result = None
            for fmt in ('%b/%d/%Y %H:%M:%S', '%m/%d/%Y %H:%M:%S'):
                try:
                    result = datetime.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if result is None:
                raise ValueError(f'Invalid imported date: {text}')
    if result.tzinfo:
        result = result.astimezone(datetime.timezone(datetime.timedelta(hours=3)))
    return result.replace(tzinfo=None, microsecond=0)


def import_now(cursor):
    cursor.execute('SELECT CURRENT_TIMESTAMP AS import_now')
    row = cursor.fetchone()
    return parse_import_datetime(row['import_now'] if isinstance(row, dict) else row[0])


def validity_duration(plan):
    value = int(present_value(plan, 'validity_value', present_value(plan, 'validity_days', 30)))
    if value < 0:
        raise ValueError('Imported validity must not be negative')
    if value == 0:
        return None
    unit = plan.get('validity_unit') or 'days'
    if unit == 'months':
        return datetime.timedelta(days=value * 30)
    if unit not in ('days', 'hours', 'minutes'):
        raise ValueError(f'Unsupported imported validity unit: {unit}')
    return datetime.timedelta(**{unit: value})


def imported_state(row, plan, now, preserve_history):
    """Fresh accounts await activation; imported history starts an existing cycle."""
    up_bytes = int(row.get('upload_used_bytes') or float(row.get('upload_used_mb') or 0) * 1048576)
    down_bytes = int(row.get('download_used_bytes') or float(row.get('download_used_mb') or 0) * 1048576)
    seconds = int(row.get('uptime_used_seconds') or int(row.get('uptime_used_mins') or 0) * 60)
    if min(up_bytes, down_bytes, seconds) < 0:
        raise ValueError('Imported consumption must not be negative')
    duration = validity_duration(plan)
    first = parse_import_datetime(row.get('first_used_at')) if preserve_history else None
    expiry = parse_import_datetime(row.get('expires_at') or row.get('till_time')) if preserve_history else None
    history = preserve_history and bool(first or expiry or up_bytes or down_bytes or seconds)
    if history:
        if first is None:
            first = expiry - duration if expiry and duration else now - datetime.timedelta(seconds=max(1, seconds))
            first = min(first, now)
        if first > now:
            raise ValueError('Imported first-use date is in the future')
        if expiry is None and duration is not None:
            expiry = first + duration
    else:
        up_bytes = down_bytes = seconds = 0
    status = 'active' if history else 'inactive'
    quota = int(plan.get('volume_quota_mb') or 0)
    uptime_limit = int(plan.get('uptime_limit_mins') or 0)
    if history and ((expiry and expiry <= now) or (quota > 0 and up_bytes + down_bytes >= quota * 1048576)
                    or (uptime_limit > 0 and seconds >= uptime_limit * 60)):
        status = 'expired'
    if row.get('disabled'):
        status = 'suspended'
    return dict(status=status, first_used_at=first, expires_at=expiry,
                last_renewed_at=first, history=history,
                upload_bytes=up_bytes, download_bytes=down_bytes, uptime_seconds=seconds)


def historical_accounting_values(username, state, now, mac=''):
    unique = uuid.uuid4().hex
    up, down = state['upload_bytes'], state['download_bytes']
    # Canonical 64-bit totals; zero legacy high words to prevent old views adding them twice.
    return (f'IMPORT-{unique}', unique, username, '', '127.0.0.1', '0', 'Wireless-802.11',
            state['first_used_at'], now, now, 0, state['uptime_seconds'], 'RADIUS', '', '',
            up, down, 0, 0, '', mac or '',
            'Consolidated-Historical-Import', 'Framed-User', 'PPP', '')


def imported_radius_checks(username, password, state):
    if state['status'] in ('disabled', 'suspended', 'expired'):
        return [(username, 'Auth-Type', ':=', 'Reject')]
    checks = [(username, 'Cleartext-Password', ':=', password)]
    if state['expires_at']:
        checks.append((username, 'Expiration', ':=', state['expires_at'].strftime('%d %b %Y %H:%M:%S')))
    return checks


def ensure_import_package_radius(conn, package_ids):
    """Initialize missing package policies without overwriting existing NAS policies."""
    from database.db import adapt_query
    from core.radius_sync import sync_package_to_radius
    cursor = conn.cursor()
    try:
        for package_id in set(package_ids):
            cursor.execute(adapt_query('SELECT name FROM wisp_packages WHERE id=?', conn), (package_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError('Imported package does not exist')
            name = row['name'] if isinstance(row, dict) else row[0]
            cursor.execute(adapt_query('SELECT groupname FROM radgroupcheck WHERE groupname=? '
                                      'UNION ALL SELECT groupname FROM radgroupreply WHERE groupname=? LIMIT 1', conn), (name, name))
            if not cursor.fetchone() and not sync_package_to_radius(package_id, conn=conn):
                raise RuntimeError('Could not initialize imported RADIUS package policy')
    finally:
        cursor.close()
