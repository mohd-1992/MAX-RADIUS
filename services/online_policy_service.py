"""Tenant max preview: real NAS checkpoints and per-session verified CoA.

Never synthesizes Accounting-Stop. Verified Hotspot detachment seals final NAS counters.
"""
import datetime
import hashlib
import hmac
import logging
import os
import re
import socket
import struct
import threading
import time
from contextlib import contextmanager

from database.db import get_connection, query_all, query_one, execute_write

log = logging.getLogger(__name__)
local = threading.local()


def fail():
    from services.renewal_settlement_service import SettlementPending
    raise SettlementPending('\u062a\u0639\u0630\u0631 \u062a\u062b\u0628\u064a\u062a \u0639\u062f\u0627\u062f\u0627\u062a \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0623\u0648 \u062a\u062d\u062f\u064a\u062b \u062d\u062f\u0648\u062f \u0627\u0644\u0631\u0627\u0648\u062a\u0631\u061b \u0644\u0645 \u062a\u0646\u0641\u0630 \u0627\u0644\u0639\u0645\u0644\u064a\u0629 \u0648\u0644\u0645 \u064a\u062a\u0645 \u0627\u0644\u062e\u0635\u0645.')


def seconds(value):
    value = str(value or '0s')
    if ':' in value:
        days = re.match(r'(?:(\d+)d)?(.*)', value)
        parts = [int(p) for p in days[2].split(':')]
        return int(days[1] or 0)*86400 + sum(v*m for v, m in zip(reversed(parts), (1,60,3600)))
    return sum(int(n)*{'w':604800,'d':86400,'h':3600,'m':60,'s':1}[u]
               for n,u in re.findall(r'(\d+)([wdhms])', value))


def rows_for(username, conn=None, locked=False):
    sql = 'SELECT * FROM radacct WHERE username=%s AND acctstoptime IS NULL'
    if conn:
        with conn.cursor() as cur:
            cur.execute(sql + (' FOR UPDATE' if locked else ''), (username,))
            return cur.fetchall()
    return query_all(sql, (username,))


def checkpoint(username):
    """Require a complete, uniquely matched NAS view; stale records are not Stops."""
    from core.mikrotik_api import RouterOSApiProtocol
    from services.nas_session_settlement import recover_journals
    try:
        recover_journals(username)
    except Exception:
        fail()
    rows = rows_for(username)
    # Inspect known NAS even when Start has not arrived, to avoid overlooking a login.
    nas_ips = {r['nasipaddress'] for r in rows}
    nas_ips.update(r['nasipaddress'] for r in query_all(
        "SELECT DISTINCT nasipaddress FROM radpostauth WHERE username=%s AND reply='Access-Accept' AND authdate>=NOW()-INTERVAL 5 MINUTE", (username,)) if r['nasipaddress'])
    result = {}
    now = query_one('SELECT NOW() AS now')['now']
    for ip in nas_ips:
        n = query_one('SELECT * FROM wisp_nas_devices WHERE ip_address=%s', (ip,))
        if not n or not n.get('api_username') or not n.get('secret'):
            fail()
        api = RouterOSApiProtocol(ip, port=n['api_port'], timeout=5)
        try:
            api.connect(); api.login(n['api_username'], n['api_password'])
            hot = api.execute_command('/ip/hotspot/active/print', ['?user='+username])
            ppp = api.execute_command('/ppp/active/print', ['?name='+username])
            if ppp:
                interfaces=api.execute_command('/interface/pppoe-server/print',['?user='+username])
                stats=api.execute_command('/interface/print',['=stats='])
                for live in ppp:
                    sid=str(live.get('session-id','')).removeprefix('0x').lower()
                    matches=[r for r in rows if r['nasipaddress']==ip and str(r['acctsessionid']).lower()==sid and r['framedipaddress']==live.get('address')]
                    links=[r for r in interfaces if str(r.get('remote-address','')).upper()==str(live.get('caller-id','')).upper() and r.get('running')=='true']
                    counters=[r for r in stats if links and r.get('.id')==links[0].get('.id')]
                    if len(matches)!=1 or len(links)!=1 or len(counters)!=1 or live.get('radius')!='true':
                        fail()
                    r=matches[0];counter=counters[0]
                    if abs((now-r['acctstarttime']).total_seconds()-seconds(live['uptime']))>15:
                        fail()
                    # Existing PPP directional limits must be unlimited; the existing
                    # global quota/expiry sweeper remains its byte enforcement owner.
                    if int(live.get('limit-bytes-in',0)) or int(live.get('limit-bytes-out',0)):
                        fail()
                    result[r['radacctid']]={'row':r,'nas':n,'live':live,'kind':'pppoe',
                        'input':int(counter['rx-byte']),'output':int(counter['tx-byte']),
                        'seconds':seconds(live['uptime'])}
            for live in hot:
                matches = [r for r in rows if r['nasipaddress']==ip
                    and r['framedipaddress']==live.get('address')
                    and str(r['callingstationid']).upper().replace('-',':')==str(live.get('mac-address')).upper()]
                if len(matches)!=1 or live.get('radius')!='true':
                    fail()
                r=matches[0]
                age = (now-r['acctstarttime']).total_seconds()
                if abs(age-seconds(live['uptime']))>15:
                    fail()
                if 'limit-bytes-total' not in live:
                    # Missing means unlimited; zero is the explicit wire encoding.
                    live['limit-bytes-total']='0'
                result[r['radacctid']]={'row':r, 'nas':n, 'live':live,
                    'kind':'hotspot','input':int(live['bytes-in']), 'output':int(live['bytes-out']),
                    'seconds':seconds(live['uptime'])}
        except Exception:
            fail()
        finally:
            api.close()
    # An absent NAS entry alone supplies no missing lifetime counters.
    # Only actual Stop or a verified frozen seal may settle such a record.
    for row in rows:
        if row['radacctid'] not in result:
            latest = query_one('SELECT acctstoptime FROM radacct WHERE radacctid=%s', (row['radacctid'],))
            if latest and latest['acctstoptime'] is None:
                fail()
    return result


@contextmanager
def view(snapshot):
    previous=getattr(local,'snapshot',None)
    previous_cuts=getattr(local,'cuts',None)
    local.snapshot=snapshot;local.cuts={}
    try:
        yield
    finally:
        local.snapshot=previous;local.cuts=previous_cuts


def overlay_totals(original, username, since_timestamp=None, conn=None, lock_for_update=False):
    snap=getattr(local,'snapshot',None)
    if snap is None:
        return original(username,since_timestamp,conn,lock_for_update)
    owned=conn is None
    db=conn or get_connection()
    try:
        # Lock before aggregation: the cut and its baseline use identical counters.
        locked=rows_for(username,db,True)
        totals=original(username,since_timestamp,db,True)
        for r in locked:
            s=snap.get(r['radacctid'])
            if not s:
                continue
            with db.cursor() as cur:
                cur.execute('SELECT * FROM wisp_session_baselines WHERE radacctid=%s AND renewed_at=%s', (r['radacctid'],since_timestamp))
                b=cur.fetchone() or {}
            cut=(max(int(r['acctinputoctets'] or 0),s['input']),
                 max(int(r['acctoutputoctets'] or 0),s['output']),
                 max(int(r['acctsessiontime'] or 0),s['seconds']))
            local.cuts[r['radacctid']]=cut
            deltas=[]
            for value,field,baseline in zip(cut,('acctinputoctets','acctoutputoctets','acctsessiontime'),
                    ('baseline_input_bytes','baseline_output_bytes','baseline_seconds')):
                base=int(b.get(baseline) or 0)
                deltas.append(max(0,value-base)-max(0,int(r[field] or 0)-base))
            totals['up_bytes']+=deltas[0];totals['down_bytes']+=deltas[1]
            totals['total_bytes']+=deltas[0]+deltas[1];totals['uptime_secs']+=deltas[2]
        return totals
    finally:
        if owned:
            db.rollback();db.close()


def overlay_baselines(original,username,renewed_at=None,conn=None):
    original(username,renewed_at,conn)
    if getattr(local,'snapshot',None) is None:
        return
    if conn is None:
        raise RuntimeError('Live baselines require the billing transaction')
    with conn.cursor() as cur:
        for rid,s in local.snapshot.items():
            if s['row']['username'].casefold()!=username.casefold():
                continue
            cut=local.cuts.get(rid)
            if cut is None:
                cur.execute('SELECT * FROM radacct WHERE radacctid=%s AND username=%s FOR UPDATE',(rid,username))
                r=cur.fetchone()
                if not r:
                    raise RuntimeError('Session disappeared while creating the cycle cut')
                cut=(max(int(r['acctinputoctets'] or 0),s['input']),max(int(r['acctoutputoctets'] or 0),s['output']),max(int(r['acctsessiontime'] or 0),s['seconds']))
            cur.execute('''UPDATE wisp_session_baselines SET baseline_input_bytes=%s,
                baseline_output_bytes=%s,baseline_bytes=%s,baseline_seconds=%s
                WHERE radacctid=%s AND username=%s AND renewed_at=%s''',
                (cut[0],cut[1],cut[0]+cut[1],cut[2],rid,username,renewed_at))


class CoARejected(RuntimeError):
    def __init__(self, cause):
        self.cause = cause
        super().__init__('NAS rejected CoA (Error-Cause=%s)' % cause)


def _coa_response(response, ident, auth, secret):
    if len(response) < 20:
        return False
    code, ri, length = struct.unpack('!BBH', response[:4])
    if (length != len(response) or ri != ident or code not in (44, 45)
            or not hmac.compare_digest(response[4:20], hashlib.md5(
                response[:4] + auth + response[20:] + secret).digest())):
        return False
    cause = None
    offset = 20
    while offset < length:
        if offset + 2 > length:
            return False
        kind, size = response[offset:offset+2]
        if size < 2 or offset + size > length:
            return False
        if kind == 101:
            if size != 6:
                return False
            cause = struct.unpack('!I', response[offset+2:offset+size])[0]
        offset += size
    if code == 45:
        raise CoARejected(cause)
    return True


def coa(session,limit=None,timeout=None,rate=None,directional_noop=False):
    r,n=session['row'],session['nas']
    def attr(t,value):
        return bytes([t,len(value)+2])+value
    attrs=attr(1,r['username'].encode())+attr(44,r['acctsessionid'].encode())+attr(8,socket.inet_aton(r['framedipaddress']))
    if limit is not None:
        for t,v in ((17,limit&0xffffffff),(18,limit>>32)):
            attrs+=attr(26,struct.pack('!I',14988)+bytes([t,6])+struct.pack('!I',v))
    if directional_noop:
        for t in (1,2):
            attrs+=attr(26,struct.pack('!I',14988)+bytes([t,6])+struct.pack('!I',0))
    if timeout is not None:
        attrs+=attr(27,struct.pack('!I',timeout))
    if rate:
        encoded=rate.encode()
        attrs+=attr(26,struct.pack('!I',14988)+bytes([8,len(encoded)+2])+encoded)
    # RFC 5176: Message-Authenticator uses a zero request authenticator;
    # then calculate the Request Authenticator over the completed attributes.
    ident=os.urandom(1)[0];secret=n['secret'].encode()
    attrs+=attr(80,b'\0'*16)
    header=struct.pack('!BBH',43,ident,20+len(attrs))
    attrs=attrs[:-16]+hmac.new(secret,header+b'\0'*16+attrs,hashlib.md5).digest()
    auth=hashlib.md5(header+b'\0'*16+attrs+secret).digest()
    # Retransmit the identical request on the same socket: no second billing,
    # no new session identity, and the NAS can recognize a duplicate request.
    packet = header + auth + attrs
    budget = getattr(local, 'coa_deadline', None) or time.monotonic() + 8
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sk:
        sk.connect((r['nasipaddress'],int(n.get('coa_port') or 3799)))
        for wait in (1.5, 2.5, 3):
            deadline = min(budget, time.monotonic() + wait)
            if deadline <= time.monotonic():
                break
            sk.send(packet)
            while time.monotonic() < deadline:
                sk.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    response = sk.recv(4096)
                except socket.timeout:
                    break
                if _coa_response(response, ident, auth, secret):
                    return
    raise TimeoutError('No verified CoA reply within the retry budget')


def preflight(snapshot):
    previous = getattr(local, 'coa_deadline', None)
    local.coa_deadline = time.monotonic() + 12
    try:
        for rid,s in list(snapshot.items()):
            if rid not in snapshot:
                continue
            # Probe with the existing byte ceiling: no change before payment.
            if s.get('kind')=='pppoe':
                coa(s,directional_noop=True)
            else:
                try:
                    coa(s,limit=int(s['live']['limit-bytes-total']))
                except CoARejected as exc:
                    if exc.cause != 406:
                        raise
                    from services.nas_session_settlement import freeze_and_disconnect
                    freeze_and_disconnect(snapshot)
    except (TimeoutError, OSError, CoARejected) as exc:
        log.warning('Online policy preflight rejected (%s)', str(exc))
        from services.renewal_settlement_service import SettlementPending
        raise SettlementPending('تعذر تأكيد تحديث جلسات الراوتر؛ لم تُنفذ العملية ولم يُخصم الرصيد. أعد المحاولة لاحقًا.') from exc
    finally:
        local.coa_deadline = previous


def intent(username):
    # Created before billing. After a crash, reconcile the committed authoritative
    # account, not a stale payload, under the same per-account named lock.
    execute_write('''INSERT INTO wisp_online_policy_outbox(username,status,next_attempt)
        VALUES(%s,'pending',NOW()) ON DUPLICATE KEY UPDATE status='pending',next_attempt=NOW(),last_error=NULL''',(username,))


def refresh(username):
    snapshot=checkpoint(username)
    with view(snapshot):
        from services.quota_service import calculate_account_quota
        q=calculate_account_quota(username)
    if not q or q['accounting_error']:
        raise RuntimeError('Quota cannot be calculated')
    if q['current_status'] in ('disabled','suspended','recharged') or q['is_exhausted']:
        # Normal expiry/disable enforcement owns termination, not this path.
        execute_write("UPDATE wisp_online_policy_outbox SET status='skipped',last_error='inactive_or_exhausted',updated_at=NOW() WHERE username=%s",(username,))
        return
    entity=query_one('SELECT * FROM wisp_subscribers WHERE username=%s',(username,))
    kind='subscriber'
    if not entity:
        entity=query_one('SELECT * FROM wisp_vouchers WHERE username=%s',(username,));kind='voucher'
    pkg=query_one('SELECT * FROM wisp_packages WHERE id=%s',(entity['package_id'],))
    rate=(entity.get('snap_rate_limit_str') if kind=='voucher' else None) or pkg.get('rate_limit_str')
    if not rate:
        from core.rate_limit import build_mikrotik_rate_limit
        rate=build_mikrotik_rate_limit(download=pkg.get('rate_download') or '0',upload=pkg.get('rate_upload') or '0',
            burst_down=pkg.get('burst_download'),burst_up=pkg.get('burst_upload'),
            threshold_down=pkg.get('burst_threshold_down'),threshold_up=pkg.get('burst_threshold_up'),
            burst_time=pkg.get('burst_time') or 16,priority=pkg.get('priority') or 8,
            min_down=pkg.get('min_download'),min_up=pkg.get('min_upload'))
    now=query_one('SELECT NOW() AS now')['now']
    n=max(1,len(snapshot))
    remaining=int(q['remaining_bytes'] or 0)
    uptime=max(0,int(q['total_uptime_mins']*60-q['used_uptime_secs']))
    ordered=sorted(snapshot.items())
    expected_timeouts={}
    for index,(rid,s) in enumerate(ordered):
        share=remaining//n+(1 if index<remaining%n else 0)
        limit=0 if q['is_quota_unlimited'] else s['input']+s['output']+share
        if not q['is_quota_unlimited'] and share<=0:
            raise RuntimeError('No byte allowance left for every live session')
        candidates=[]
        if entity.get('expires_at'):
            candidates.append(int((entity['expires_at']-now).total_seconds()))
        if not q['is_uptime_unlimited']:
            candidates.append(uptime//n)
        timeout=max(1,min(candidates)) if candidates else 0xffffffff
        expected_timeouts[rid]=timeout
        # RouterOS 7.20.8 measures CoA Session-Timeout from session start.
        wire_timeout=min(0xffffffff,s['seconds']+timeout) if candidates else 0xffffffff
        coa(s,None if s.get('kind')=='pppoe' else limit,wire_timeout,rate)
    after=checkpoint(username)
    if set(after)!=set(snapshot):
        raise RuntimeError('Session membership changed during refresh')
    for rid,s in after.items():
        original=snapshot[rid]
        index=[v[0] for v in ordered].index(rid)
        expected=0 if q['is_quota_unlimited'] else original['input']+original['output']+remaining//n+(1 if index<remaining%n else 0)
        if s.get('kind')=='pppoe':
            continue
        if expected_timeouts[rid] < 0xffffffff and abs(seconds(s['live'].get('session-time-left'))-expected_timeouts[rid])>10:
            raise RuntimeError('NAS did not apply session timeout')
        if int(s['live']['limit-bytes-total'])!=expected:
            raise RuntimeError('NAS did not apply byte ceiling')
    execute_write("UPDATE wisp_online_policy_outbox SET status='applied',last_error=NULL,updated_at=NOW() WHERE username=%s",(username,))


def refresh_or_pending(username):
    try:
        refresh(username)
        return True
    except Exception as exc:
        # No second billing; retry the latest committed policy only.
        execute_write("UPDATE wisp_online_policy_outbox SET status='pending',last_error=%s,next_attempt=NOW()+INTERVAL 15 SECOND,updated_at=NOW() WHERE username=%s",(type(exc).__name__,username))
        log.warning('Online policy update pending; retry scheduled (%s)',type(exc).__name__)
        return False


def retry_loop():
    while True:
        time.sleep(15)
        try:
            from pathlib import Path
            import json
            recovery=set()
            root=Path(os.environ.get('MAX_SETTLEMENT_JOURNAL_DIR','/app/storage/nas-session-settlements'))
            for path in list(root.glob('*.json'))[:10]:
                recovery.add(json.loads(path.read_text())['username'])
            pending=query_all("SELECT username FROM wisp_online_policy_outbox WHERE status='pending' AND next_attempt<=NOW() LIMIT 10")
            for row in pending+[{'username':u,'recovery_only':True} for u in recovery]:
                username=row['username'];db=get_connection()
                lock='max-cycle:'+hashlib.sha256(username.casefold().encode()).hexdigest()[:48]
                try:
                    with db.cursor() as cur:
                        cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock,))
                        if cur.fetchone()['acquired']!=1:
                            continue
                    if row.get('recovery_only'):
                        from services.nas_session_settlement import recover_journals
                        recover_journals(username)
                    else:
                        refresh_or_pending(username)
                finally:
                    with db.cursor() as cur:
                        cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
                    db.close()
        except Exception:
            log.warning('Online policy retry unavailable')


threading.Thread(target=retry_loop,name='MaxOnlinePolicyRetry',daemon=True).start()
