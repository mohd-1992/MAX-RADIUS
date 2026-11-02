"""Explicit disposable MariaDB + two actual FreeRADIUS images, never a real NAS.

Run inside the pinned Web image with --run, DB_NAME=acct_events_fixture,
ACCT_EVENT_OLD_CORE, ACCT_EVENT_NEW_CORE, and ACCT_EVENT_SECRET. The runtime
license lease is synthetic; crypto license acceptance belongs to its own suite.
Results contain aggregates only. Never replay an attempted lab directory.
"""
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time


def main():
    assert sys.argv[1:] == ['--run']
    assert os.environ['DB_NAME'] == 'acct_events_fixture'
    marker = Path('/lab/PACKETS-ATTEMPTED.private')
    assert not marker.exists(), 'Inspect receipts; do not replay financial/accounting drivers'
    marker.touch()
    threading.Thread.start = lambda self: None
    sys.path.insert(0, '/app')
    from database.db import execute_write, query_one, query_all
    from core.radius_sync import sync_package_to_radius, sync_subscriber_to_radius

    secret = os.environ['ACCT_EVENT_SECRET'].encode()
    checks = []
    packet_count = 0
    classes = {}

    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
        Path('/lab/RESULT.private.json').write_text(json.dumps({'checks': checks, 'packets': packet_count}))
        assert condition, name

    def attr(code, value):
        if isinstance(value, str):
            value = value.encode()
        return bytes((code, len(value) + 2)) + value

    def packet(host, user='', status=None, sid='event', nas='192.0.2.41',
               port=1, stamp=None, seconds=0, up=0, down=0, klass=None):
        nonlocal packet_count
        ident = os.urandom(1)[0]
        auth = os.urandom(16) if status is None else bytes(16)
        data = attr(4, socket.inet_aton(nas))
        if user:
            data += attr(1, user) + attr(5, struct.pack('!I', port))
            data += attr(61, struct.pack('!I', 15 if port == 1 else 19))
            data += attr(31, '02:AC:26:90:00:' + str(port).zfill(2))
            data += attr(30, 'lab-hotspot' if port == 1 else 'lab-pppoe')
            data += attr(8, socket.inet_aton('10.249.0.' + str(port)))
        if stamp is not None:
            data += attr(55, struct.pack('!I', stamp))
            data += attr(41, struct.pack('!I', max(0, int(time.time()) - stamp)))
        if status is not None:
            data += attr(40, struct.pack('!I', status))
            if user:
                data += attr(44, sid) + attr(46, struct.pack('!I', seconds))
                for key, value in ((42, up % 2**32), (43, down % 2**32),
                                   (52, up // 2**32), (53, down // 2**32)):
                    data += attr(key, struct.pack('!I', value))
                if status == 2:
                    data += attr(49, struct.pack('!I', 1))
            klass = klass or classes.get(user)
            if klass:
                data += attr(25, klass)
            code = 4
        else:
            password = b'fixture-only'.ljust(16, b'\0')
            mask = hashlib.md5(secret + auth).digest()
            data += attr(2, bytes(x ^ y for x, y in zip(password, mask)))
            data += attr(80, bytes(16))
            code = 1
        header = struct.pack('!BBH', code, ident, 20 + len(data))
        if code == 4:
            auth = hashlib.md5(header + auth + data + secret).digest()
        else:
            data = data[:-16] + hmac.new(secret, header + auth + data, hashlib.md5).digest()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(6)
            sock.sendto(header + auth + data, (host, 1813 if status is not None else 1812))
            response = sock.recv(65535)
        assert response[1] == ident
        assert hmac.compare_digest(response[4:20], hashlib.md5(response[:4] + auth + response[20:] + secret).digest())
        packet_count += 1
        attrs = {}; pos = 20
        while pos < len(response):
            kind, size = response[pos:pos+2]
            attrs[kind] = response[pos+2:pos+size]; pos += size
        assert response[0] == 5 if status is not None else response[0] in (2, 3)
        return response[0], attrs

    def row(user):
        return query_one('SELECT * FROM radacct WHERE username=%s ORDER BY radacctid DESC LIMIT 1', (user,))

    pid = execute_write("INSERT INTO wisp_packages(name,service_type,simultaneous_sessions,volume_quota_mb,validity_value,validity_unit,is_active) VALUES('Accounting event fixture','both',1,0,30,'days',1)")
    sync_package_to_radius(pid)
    for name in ('event-old', 'event-hs', 'event-ppp', 'event-other', 'event-reboot'):
        sid = execute_write("INSERT INTO wisp_subscribers(username,password,full_name,service_type,package_id,status,balance,first_used_at,last_renewed_at,expires_at) VALUES(%s,'fixture-only','Isolated event acceptance','hotspot',%s,'active',123.45,NOW()-INTERVAL 1 DAY,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 30 DAY)", (name, pid))
        sync_subscriber_to_radius(sid)
    # Explicit bounded capacity fixture, not issuer acceptance or an auth bypass.
    execute_write("INSERT INTO wisp_license_info(license_id,client_name,raw_package_json,signature_b64,status) VALUES('accounting-event-fixture','Synthetic','{}','fixture-only','active')")
    license_id = query_one("SELECT id FROM wisp_license_info WHERE license_id='accounting-event-fixture'")['id']
    execute_write("INSERT INTO wisp_license_runtime_state(id,license_row_id,package_hash,is_valid,license_mode,max_active_sessions,valid_until) VALUES(1,%s,SHA2('{}',256),1,'active_sessions',1000,UTC_TIMESTAMP()+INTERVAL 1 HOUR) ON DUPLICATE KEY UPDATE license_row_id=VALUES(license_row_id),package_hash=VALUES(package_hash),is_valid=1,license_mode='active_sessions',max_active_sessions=1000,valid_until=VALUES(valid_until)", (license_id,))
    old = os.environ['ACCT_EVENT_OLD_CORE']; new = os.environ['ACCT_EVENT_NEW_CORE']
    now = int(time.time())
    packet(old, 'event-old', 1, sid='old', stamp=now-10)
    packet(old, status=7, stamp=now)
    check('old-image-reproduces-premature-NAS-wide-closure', row('event-old')['acctterminatecause'] == 'NAS-Reboot' and row('event-old')['acctstoptime'] is not None)

    for user, port, nas in (('event-hs', 1, '192.0.2.41'), ('event-ppp', 2, '192.0.2.41'), ('event-other', 3, '192.0.2.42')):
        code, reply = packet(new, user, port=port, nas=nas)
        check(user+'-actual-authentication', code == 2)
        classes[user] = reply.get(25)
        packet(new, user, 1, sid=user, port=port, nas=nas, stamp=now-5, klass=classes[user])
    baseline = {u:row(u) for u in ('event-hs', 'event-ppp', 'event-other')}
    financial = query_all('SELECT id,balance,package_id,last_renewed_at,expires_at FROM wisp_subscribers ORDER BY id')
    for status in (7, 8, 7):
        packet(new, status=status, stamp=now)
        check('status-'+str(status)+'-preserves-all-session-fields-'+str(packet_count), all(row(u) == v for u,v in baseline.items()))
    marker_time = query_one("SELECT reloadtime FROM nasreload WHERE nasipaddress='192.0.2.41'")['reloadtime']
    packet(new, status=7, stamp=now-3600)
    check('delayed-event-does-not-rewind-marker', query_one("SELECT reloadtime FROM nasreload WHERE nasipaddress='192.0.2.41'")['reloadtime'] == marker_time)
    check('diagnostic-marker-is-one-row-per-NAS', query_one("SELECT COUNT(*) n FROM nasreload WHERE nasipaddress='192.0.2.41'")['n'] == 1)
    code, _ = packet(new, 'event-hs', port=4)
    check('on-off-does-not-release-account-session-limit', code == 3)
    packet(new, 'event-hs', 3, sid='event-hs', stamp=now+10, seconds=15, up=2**32+345, down=2**33+789)
    check('64-bit-interim-continues-after-event', row('event-hs')['acctinputoctets'] == 2**32+345 and row('event-hs')['acctoutputoctets'] == 2**33+789 and row('event-hs')['acctstoptime'] is None)
    packet(new, 'event-hs', 2, sid='event-hs', stamp=now+20, seconds=25, up=2**32+999, down=2**33+999)
    stopped = row('event-hs')
    check('genuine-stop-closes-only-target', stopped['acctterminatecause'] == 'User-Request' and stopped['acctstoptime'] is not None and row('event-ppp') == baseline['event-ppp'])
    packet(new, status=8, stamp=now+21)
    check('event-after-stop-preserves-final-row', row('event-hs') == stopped)
    packet(new, 'event-hs', 3, sid='event-hs', stamp=now+10, seconds=15, up=123, down=456)
    check('delayed-interim-does-not-reopen-or-decrease-final-counters', row('event-hs') == stopped)
    packet(new, 'event-hs', 2, sid='event-hs', stamp=now+20, seconds=25, up=2**32+999, down=2**33+999)
    check('duplicate-stop-does-not-duplicate-session', query_one("SELECT COUNT(*) n FROM radacct WHERE username='event-hs'")['n'] == 1)
    check('financial-contracts-unchanged', query_all('SELECT id,balance,package_id,last_renewed_at,expires_at FROM wisp_subscribers ORDER BY id') == financial)

    # A reboot event without Stop is pending, not fabricated final accounting.
    packet(new, 'event-reboot', 1, sid='reboot', port=5, stamp=now-1200)
    packet(new, status=7, stamp=now+22)
    check('reboot-without-stop-remains-pending-until-existing-timeout', row('event-reboot')['acctstoptime'] is None)
    from services.subscriber_service import clean_stale_sessions
    closed = clean_stale_sessions(timeout_minutes=5)
    check('existing-stale-lifecycle-closes-missing-stop', closed >= 1 and row('event-reboot')['acctterminatecause'] == 'Stale-Session-Timeout')
    check('timeout-does-not-close-fresh-peer', row('event-ppp')['acctstoptime'] is None)
    packet(new, 'event-reboot', 2, sid='reboot', port=5, stamp=now+23, seconds=1223, up=9999, down=8888)
    check('genuine-delayed-stop-refines-timeout', row('event-reboot')['acctterminatecause'] == 'User-Request' and row('event-reboot')['acctinputoctets'] == 9999)
    for user, port, nas in (('event-ppp',2,'192.0.2.41'), ('event-other',3,'192.0.2.42')):
        packet(new,user,2,sid=user,port=port,nas=nas,stamp=now+25,seconds=30,up=1000,down=2000)
    check('all-candidate-test-sessions-have-genuine-final-stops', query_one("SELECT COUNT(*) n FROM radacct WHERE username LIKE %s AND username<>'event-old' AND acctstoptime IS NULL", ('event-%',))['n'] == 0)
    print(json.dumps({'passed':len(checks),'packets':packet_count,'license_scope':'synthetic bounded runtime lease; actual Core auth/accounting; no NAS'}))


if __name__ == '__main__':
    main()
