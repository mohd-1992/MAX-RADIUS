# -*- coding: utf-8 -*-
"""
Synchronizer between WISP Billing models and standard FreeRADIUS tables
(radcheck, radreply, radgroupreply, radgroupcheck, radusergroup).
"""

from database.db import db_session, adapt_query, is_mysql_conn
from core.rate_limit import build_mikrotik_rate_limit

def _write(conn, sql, params=()):
    cursor = conn.cursor()
    try:
        cursor.execute(adapt_query(sql, conn), params)
        return cursor.lastrowid
    finally:
        cursor.close()


def _one(conn, sql, params=()):
    cursor = conn.cursor()
    try:
        cursor.execute(adapt_query(sql, conn), params)
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        cursor.close()


def sync_package_to_radius(package_id, conn=None):
    """Syncs a WISP package to radgroupreply and radgroupcheck."""
    if conn is None:
        with db_session() as connection:
            return sync_package_to_radius(package_id, conn=connection)
    execute_write = lambda sql, params=(): _write(conn, sql, params)
    query_one = lambda sql, params=(): _one(conn, sql, params)

    pkg = query_one('SELECT * FROM wisp_packages WHERE id = ?', (package_id,))
    if not pkg:
        return False
    
    groupname = pkg['name']
    
    # Clean previous group attributes
    execute_write('DELETE FROM radgroupreply WHERE groupname = ?', (groupname,))
    execute_write('DELETE FROM radgroupcheck WHERE groupname = ?', (groupname,))
    
    # 1. Build MikroTik-Rate-Limit (Skip if 0 = Unlimited to avoid creating dynamic Simple Queues)
    rate_str = build_mikrotik_rate_limit(
        download=pkg['rate_download'],
        upload=pkg['rate_upload'],
        burst_down=pkg.get('burst_download'),
        burst_up=pkg.get('burst_upload'),
        threshold_down=pkg.get('burst_threshold_down'),
        threshold_up=pkg.get('burst_threshold_up'),
        burst_time=pkg.get('burst_time', 16),
        priority=pkg.get('priority', 8),
        min_down=pkg.get('min_download'),
        min_up=pkg.get('min_upload')
    )
    
    if rate_str:
        execute_write(
            'INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (?, ?, ?, ?)',
            (groupname, 'MikroTik-Rate-Limit', '=', rate_str)
        )
    
    # 2. Interim Accounting (keep alive & quota tracking every 180 seconds / 3 minutes)
    execute_write(
        'INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (?, ?, ?, ?)',
        (groupname, 'Acct-Interim-Interval', '=', '180')
    )
    
    # 3. Simultaneous Use Limit
    simul = str(pkg.get('simultaneous_sessions') or 1)
    execute_write(
        'INSERT INTO radgroupcheck (groupname, attribute, op, value) VALUES (?, ?, ?, ?)',
        (groupname, 'Simultaneous-Use', ':=', simul)
    )

    # 4. Volume Quota Limits are handled dynamically per-user in SQL authorize_reply_query (Remaining Quota)

    # 5. Hotspot User Profile Binding (Mikrotik-Group VSA from package definition)
    mgroup = pkg.get('mikrotik_group') and str(pkg['mikrotik_group']).strip()
    if mgroup:
        execute_write(
            'INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (?, ?, ?, ?)',
            (groupname, 'Mikrotik-Group', '=', str(mgroup))
        )
        
    return True

def sync_subscriber_to_radius(subscriber_id, conn=None):
    """Syncs a subscriber credentials and attributes to radcheck, radreply, radusergroup."""
    if conn is None:
        with db_session() as connection:
            return sync_subscriber_to_radius(subscriber_id, conn=connection)
    execute_write = lambda sql, params=(): _write(conn, sql, params)
    query_one = lambda sql, params=(): _one(conn, sql, params)

    sub = query_one('''
        SELECT s.*, p.name as package_name, p.mikrotik_group as pkg_mikrotik_group 
        FROM wisp_subscribers s 
        JOIN wisp_packages p ON s.package_id = p.id 
        WHERE s.id = ?
    ''' + (' FOR UPDATE' if is_mysql_conn(conn) else ''), (subscriber_id,))
    if not sub:
        return False
        
    username = sub['username']
    
    # Clean previous records
    execute_write('DELETE FROM radcheck WHERE username = ?', (username,))
    execute_write('DELETE FROM radreply WHERE username = ?', (username,))
    execute_write('DELETE FROM radusergroup WHERE username = ?', (username,))
    
    if sub['status'] != 'active':
        return True
        
    # 1. Cleartext-Password
    execute_write(
        'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
        (username, 'Cleartext-Password', ':=', sub['password'])
    )
    
    # 2. MAC Binding if present
    if sub.get('mac_binding') and len(sub['mac_binding'].strip()) > 5:
        clean_mac = sub['mac_binding'].strip().replace('-', ':').upper()
        execute_write(
            'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
            (username, 'Calling-Station-Id', '==', clean_mac)
        )
        
    # 3. Static IP in radreply if present
    if sub.get('static_ip') and len(sub['static_ip'].strip()) > 6:
        execute_write(
            'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)',
            (username, 'Framed-IP-Address', ':=', sub['static_ip'].strip())
        )
        
    # 4. Group Assignment
    execute_write(
        'INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)',
        (username, sub['package_name'], 1)
    )
    
    # 5. Mikrotik-Group from Subscriber or Package
    mgroup = (sub.get('mikrotik_group') and str(sub['mikrotik_group']).strip()) or (sub.get('pkg_mikrotik_group') and str(sub['pkg_mikrotik_group']).strip())
    if mgroup:
        execute_write(
            'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)',
            (username, 'Mikrotik-Group', ':=', str(mgroup))
        )

    # 6. Expiration attribute if active and has expiry
    if sub.get('expires_at'):
        try:
            import datetime
            exp_val = sub['expires_at']
            if isinstance(exp_val, str):
                exp_dt = datetime.datetime.fromisoformat(exp_val.replace('Z', ''))
            elif isinstance(exp_val, datetime.datetime):
                exp_dt = exp_val
            else:
                exp_dt = None
            if exp_dt:
                freeradius_exp = exp_dt.strftime('%d %b %Y %H:%M:%S')
                execute_write(
                    'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
                    (username, 'Expiration', ':=', freeradius_exp)
                )
        except Exception as e:
            raise

    return True

def sync_voucher_to_radius(voucher_id, conn=None):
    """Syncs a voucher card to radcheck, radreply, and radusergroup based on activation state."""
    if conn is None:
        with db_session() as connection:
            return sync_voucher_to_radius(voucher_id, conn=connection)
    execute_write = lambda sql, params=(): _write(conn, sql, params)
    query_one = lambda sql, params=(): _one(conn, sql, params)

    v = query_one('''
        SELECT v.*, 
               p.name as package_name,
               p.rate_download as pkg_rate_download,
               p.rate_upload as pkg_rate_upload,
               p.simultaneous_sessions as pkg_simultaneous_sessions,
               p.mikrotik_group as pkg_mikrotik_group
        FROM wisp_vouchers v 
        JOIN wisp_packages p ON v.package_id = p.id 
        WHERE v.id = ?
    ''' + (' FOR UPDATE' if is_mysql_conn(conn) else ''), (voucher_id,))
    if not v:
        return False
        
    username = v['username']
    
    execute_write('DELETE FROM radcheck WHERE username = ?', (username,))
    execute_write('DELETE FROM radreply WHERE username = ?', (username,))
    execute_write('DELETE FROM radusergroup WHERE username = ?', (username,))
    
    if v['status'] in ['expired', 'disabled', 'used', 'suspended']:
        return True
        
    # 1. Password (Simultaneous-Use is handled in radgroupcheck for the package group)
    execute_write(
        'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
        (username, 'Cleartext-Password', ':=', v['password'])
    )

    # 2. If active, write frozen rate-limit directly in radreply
    if v['status'] == 'active':
        rate_str = v.get('snap_rate_limit_str')
        if not rate_str and (v.get('snap_rate_download') or v.get('snap_rate_upload')):
            rate_str = build_mikrotik_rate_limit(download=v.get('snap_rate_download', 0), upload=v.get('snap_rate_upload', 0))
        if rate_str:
            execute_write(
                'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)',
                (username, 'MikroTik-Rate-Limit', ':=', rate_str)
            )

        # Interim Accounting
        execute_write(
            'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)',
            (username, 'Acct-Interim-Interval', ':=', '180')
        )

        # Hotspot User Profile Binding (Mikrotik-Group VSA from Voucher or Package)
        mgroup = v.get('snap_mikrotik_group') or (v.get('pkg_mikrotik_group') and str(v['pkg_mikrotik_group']).strip())
        if mgroup and str(mgroup).strip():
            execute_write(
                'INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)',
                (username, 'Mikrotik-Group', ':=', str(mgroup).strip())
            )
    
    # 4. Expiration attribute if active and has expiry
    if v.get('expires_at'):
        try:
            import datetime
            exp_dt = datetime.datetime.strptime(str(v['expires_at']), '%Y-%m-%d %H:%M:%S')
            freeradius_exp = exp_dt.strftime('%d %b %Y %H:%M:%S')
            execute_write(
                'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
                (username, 'Expiration', ':=', freeradius_exp)
            )
        except Exception:
            raise

    # 5. MAC Binding if already locked
    if v.get('bound_mac') and len(v['bound_mac'].strip()) > 5:
        execute_write(
            'INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)',
            (username, 'Calling-Station-Id', '==', v['bound_mac'].strip().upper())
        )
        
    # 6. Group Assignment
    if v.get('package_name'):
        execute_write(
            'INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)',
            (username, v['package_name'], 1)
        )
    return True

def delete_user_from_radius(username, conn=None):
    """Removes user completely from FreeRADIUS tables."""
    if conn is None:
        with db_session() as connection:
            return delete_user_from_radius(username, conn=connection)
    execute_write = lambda sql, params=(): _write(conn, sql, params)
    query_one = lambda sql, params=(): _one(conn, sql, params)

    execute_write('DELETE FROM radcheck WHERE username = ?', (username,))
    execute_write('DELETE FROM radreply WHERE username = ?', (username,))
    execute_write('DELETE FROM radusergroup WHERE username = ?', (username,))
