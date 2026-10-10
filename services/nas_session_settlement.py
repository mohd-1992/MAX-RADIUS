"""Freeze Hotspot traffic, verify counters and NAS absence, then seal accounting.

No RADIUS packet is fabricated. NAS-Verified-Closed is an administrative closure
with persisted evidence; the seal makes delayed packets unable to rebill it.
"""
import datetime
import logging
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from database.db import get_connection, query_all, query_one

NON_FINAL_CAUSES = (None, '', 'Admin-Reset-CoA', 'Stale-Session-Timeout',
                    'Watchdog-Autoheal-Timeout', 'Backup-Restored-Closed',
                    'NAS-Verified-Closed', 'NAS-Reboot')


def actual_stop(row):
    return bool(row and row.get('acctstoptime') and
                row.get('acctterminatecause') not in NON_FINAL_CAUSES)


def journal_path(username, token):
    root = Path(os.environ.get('MAX_SETTLEMENT_JOURNAL_DIR', '/app/storage/nas-session-settlements'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / (hashlib.sha256(username.casefold().encode()).hexdigest()+'-'+token+'.json')


def journal(snapshot, phase):
    first=next(iter(snapshot.values()))
    token=first['evidence_token']
    path=journal_path(first['row']['username'],token)
    payload={'phase':phase,'token':token,'username':first['row']['username'],'sessions':{}}
    for rid,s in snapshot.items():
        fields=('radacctid','acctuniqueid','username','nasipaddress','acctsessionid','framedipaddress','acctstarttime','callingstationid')
        payload['sessions'][str(rid)]={'row':{k:s['row'].get(k) for k in fields},
            'input':s['input'],'output':s['output'],'seconds':s['seconds'],
            'evidence_token':token,'kind':s.get('kind','hotspot'),'nas_live_id':s.get('live',{}).get('.id') or s['nas_live_id']}
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf8') as f:
        os.chmod(tmp,0o600)
        json.dump(payload,f,default=str);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    if os.name=='posix':
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return path


def command(api, path, words=None):
    result = api.execute_command(path, words)
    traps = [row.get('message', 'rejected') for row in result if '!trap' in row]
    if traps:
        raise RuntimeError('NAS rejected settlement command: ' + path + ': ' + '; '.join(traps))
    return result


def identity(live):
    return live['user'], live['address'], live['mac-address'].upper().replace('-', ':')


def counters(live):
    return int(live['bytes-in']), int(live['bytes-out'])


def live_view(api, session):
    row=session['row'];user=row['username']
    if session.get('kind','hotspot')!='pppoe':
        return command(api,'/ip/hotspot/active/print',['?user='+user])
    live=command(api,'/ppp/active/print',['?name='+user])
    interfaces=command(api,'/interface/pppoe-server/print',['?user='+user])
    stats=command(api,'/interface/print',['=stats='])
    result=[]
    for item in live:
        links=[x for x in interfaces if str(x.get('remote-address','')).upper()==str(item.get('caller-id','')).upper() and x.get('running')=='true']
        count=[x for x in stats if links and x.get('.id')==links[0]['.id']]
        if len(links)!=1 or len(count)!=1:raise RuntimeError('PPP counters unavailable')
        result.append(dict(item,user=item['name'],**{'mac-address':item.get('caller-id',''),'bytes-in':count[0]['rx-byte'],'bytes-out':count[0]['tx-byte']}))
    return result


def original_identity(session):
    live=session.get('live',{})
    if session.get('kind')=='pppoe':
        return live.get('name') or session['row']['username'],session['row']['framedipaddress'],str(live.get('caller-id') or session['row'].get('callingstationid','')).upper().replace('-',':')
    return identity(live)


def active_menu(session):
    return '/ppp/active' if session.get('kind')=='pppoe' else '/ip/hotspot/active'


def seal_rows(snapshot):
    """Commit evidence and administrative closures together, never just an ACK."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for rid, session in sorted(snapshot.items()):
                cur.execute('SELECT * FROM radacct WHERE radacctid=%s FOR UPDATE', (rid,))
                row = cur.fetchone()
                original = session['row']
                if not row or any(row[k] != original[k] for k in ('username','nasipaddress','acctsessionid','framedipaddress')):
                    raise RuntimeError('Accounting identity changed before sealing')
                if row.get('acctuniqueid')!=original.get('acctuniqueid') or str(row['acctstarttime'])!=str(original['acctstarttime']):
                    raise RuntimeError('Accounting generation changed before sealing')
                # Teardown/control packets can advance either service's counters
                # after the NAS read. A genuine Stop for this exact generation is
                # authoritative; merge monotonically before the idempotency check.
                if actual_stop(row):
                    session = dict(session, input=max(session['input'],int(row['acctinputoctets'] or 0)),
                                   output=max(session['output'],int(row['acctoutputoctets'] or 0)),
                                   seconds=max(session['seconds'],int(row['acctsessiontime'] or 0)))
                cur.execute('SELECT * FROM wisp_nas_session_seals WHERE radacctid=%s',(rid,))
                existing=cur.fetchone()
                if existing:
                    if existing['evidence_token']!=session['evidence_token'] or existing['input_bytes']!=session['input'] or existing['output_bytes']!=session['output']:
                        raise RuntimeError('Conflicting settlement evidence')
                    continue
                if session.get('kind') == 'pppoe':
                    # PPP teardown can add control bytes after interface counters
                    # stabilize. Only an actual final accounting packet can
                    # authorize using those larger counters; never discard it.
                    if not actual_stop(row):
                        raise RuntimeError('PPP final counters are not yet verified')
                    session = dict(session, input=max(session['input'],int(row['acctinputoctets'] or 0)),
                                   output=max(session['output'],int(row['acctoutputoctets'] or 0)))
                if int(row['acctinputoctets'] or 0) > session['input'] or int(row['acctoutputoctets'] or 0) > session['output']:
                    raise RuntimeError('Final accounting exceeds frozen NAS counters')
                cur.execute('''INSERT INTO wisp_nas_session_seals
                    (radacctid,username,nasipaddress,acctsessionid,input_bytes,output_bytes,session_seconds,sealed_at,evidence_token)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),%s)''',
                    (rid,row['username'],row['nasipaddress'],row['acctsessionid'],session['input'],session['output'],
                     max(int(row['acctsessiontime'] or 0),session['seconds']),session['evidence_token']))
                cur.execute('''UPDATE radacct SET acctinputoctets=%s,acctoutputoctets=%s,
                    acctsessiontime=GREATEST(COALESCE(acctsessiontime,0),%s),
                    acctstoptime=COALESCE(acctstoptime,NOW()),acctupdatetime=NOW(),
                    acctterminatecause=CASE WHEN acctstoptime IS NULL OR COALESCE(acctterminatecause,'')=''
                        THEN 'NAS-Verified-Closed' ELSE acctterminatecause END WHERE radacctid=%s''',
                    (session['input'],session['output'],session['seconds'],rid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_journals(username):
    """Recover counters/closure only; never replay a purchase or renewal."""
    from core.mikrotik_api import RouterOSApiProtocol
    root=Path(os.environ.get('MAX_SETTLEMENT_JOURNAL_DIR','/app/storage/nas-session-settlements'))
    prefix=hashlib.sha256(username.casefold().encode()).hexdigest()+'-'
    for path in root.glob(prefix+'*.json'):
        payload=json.loads(path.read_text(encoding='utf8'))
        if payload['username'].casefold()!=username.casefold():
            raise RuntimeError('Invalid settlement journal owner')
        sessions={int(rid):s for rid,s in payload['sessions'].items()}
        if payload['phase']=='frozen':
            # A verified Stop can settle an interrupted removal even after its
            # temporary firewall lease has expired. seal_rows verifies identity.
            pending={rid:s for rid,s in sessions.items() if not actual_stop(query_one(
                'SELECT acctstoptime,acctterminatecause FROM radacct WHERE radacctid=%s',(rid,)))}
            for ip in {s['row']['nasipaddress'] for s in pending.values()}:
                n=query_one('SELECT * FROM wisp_nas_devices WHERE ip_address=%s',(ip,))
                if not n:raise RuntimeError('Recovery NAS unavailable')
                api=RouterOSApiProtocol(ip,port=n['api_port'],timeout=5)
                try:
                    api.connect()
                    if not api.login(n['api_username'],n['api_password']):raise RuntimeError('Recovery NAS login failed')
                    comment='MAX-SETTLEMENT:'+payload['token']
                    rules=command(api,'/ip/firewall/raw/print',['?comment='+comment])
                    live_by_kind={kind:live_view(api,next(s for s in pending.values() if s['row']['nasipaddress']==ip and s.get('kind','hotspot')==kind)) for kind in {s.get('kind','hotspot') for s in pending.values() if s['row']['nasipaddress']==ip}}
                    for s in pending.values():
                        if s['row']['nasipaddress']!=ip:continue
                        address=s['row']['framedipaddress']+'/32'
                        if not all(any(r.get('action')=='drop' and r.get('chain')=='prerouting' and r.get(field)==address for r in rules) for field in ('src-address','dst-address')):
                            raise RuntimeError('Interrupted freeze no longer proves final counters')
                        old=[r for r in live_by_kind[s.get('kind','hotspot')] if r.get('.id')==s['nas_live_id']]
                        if old:
                            if len(old)!=1 or old[0].get('address')!=s['row']['framedipaddress'] or (s['row'].get('callingstationid') and str(old[0].get('mac-address','')).upper().replace('-',':')!=str(s['row']['callingstationid']).upper().replace('-',':')) or (s.get('kind')=='pppoe' and str(old[0].get('session-id','')).lower().removeprefix('0x')!=str(s['row']['acctsessionid']).lower().removeprefix('0x')) or counters(old[0])!=(s['input'],s['output']):
                                raise RuntimeError('Interrupted freeze counters changed')
                            from services.online_policy_service import seconds
                            s['seconds']=max(s['seconds'],seconds(old[0]['uptime']))
                            command(api,active_menu(s)+'/remove',{'numbers':s['nas_live_id']})
                    if any(r.get('.id')==s['nas_live_id'] for s in pending.values() if s['row']['nasipaddress']==ip for r in live_view(api,s)):
                        raise RuntimeError('Interrupted disconnect not verified')
                finally:api.close()
        elif payload['phase']!='detached':
            raise RuntimeError('Invalid settlement journal phase')
        journal(sessions,'detached')
        seal_rows(sessions)
        path.unlink()
        # No billing is replayed. Only the old proof and owned freeze are removed.
        for ip in {s['row']['nasipaddress'] for s in sessions.values()}:
            n=query_one('SELECT * FROM wisp_nas_devices WHERE ip_address=%s',(ip,))
            if not n:continue
            api=RouterOSApiProtocol(ip,port=n['api_port'],timeout=5)
            try:
                api.connect()
                if not api.login(n['api_username'],n['api_password']):continue
                for menu,field,value in [('/ip/firewall/raw','comment','MAX-SETTLEMENT:'+payload['token']),('/system/scheduler','name','max-seal-'+payload['token'])]:
                    for r in command(api,menu+'/print',['?'+field+'='+value]):command(api,menu+'/remove',{'numbers':r['.id']})
            except Exception:pass  # NAS-owned cleanup still bounds the freeze lease.
            finally:api.close()


def freeze_and_disconnect(snapshot, include_ppp=False):
    """Only the matched Hotspot sessions; account guard must already be held."""
    from core.mikrotik_api import RouterOSApiProtocol
    from services.online_policy_service import seconds
    from services.renewal_settlement_service import SettlementPending
    hot = {rid:s for rid,s in snapshot.items() if s.get('kind') == 'hotspot' or (include_ppp and s.get('kind')=='pppoe')}
    if not hot:
        return
    token = uuid.uuid4().hex
    apis = {}
    installed = []
    schedulers = []
    completed = {}
    proof = None
    removing = False
    sealed = False
    try:
        for ip in sorted({s['row']['nasipaddress'] for s in hot.values()}):
            n = next(s['nas'] for s in hot.values() if s['row']['nasipaddress'] == ip)
            api = RouterOSApiProtocol(ip, port=n['api_port'], timeout=5)
            api.connect()
            apis[ip] = api
            if not api.login(n['api_username'], n['api_password']):
                raise RuntimeError('NAS login failed')
            rules = command(api, '/ip/firewall/filter/print')
            if any(r.get('action') == 'fasttrack-connection' and r.get('disabled') != 'true' for r in rules):
                raise RuntimeError('FastTrack bypass prevents a verified traffic freeze')
            # Router-owned cleanup survives a web crash. Use NAS clock, not VPS clock.
            clock = command(api, '/system/clock/print')[0]
            stamp = clock['date'] + ' ' + clock['time']
            for fmt in ('%Y-%m-%d %H:%M:%S', '%b/%d/%Y %H:%M:%S'):
                try:
                    expires = datetime.datetime.strptime(stamp, fmt) + datetime.timedelta(minutes=3)
                    break
                except ValueError:
                    continue
            else:
                raise RuntimeError('Cannot schedule freeze cleanup using NAS clock')
            name = 'max-seal-' + token
            comment = 'MAX-SETTLEMENT:' + token
            event = '/ip firewall raw remove [find where comment="'+comment+'"]; /system scheduler remove [find where name="'+name+'"]'
            command(api, '/system/scheduler/add', {'name':name,'start-date':expires.strftime('%Y-%m-%d'),
                'start-time':expires.strftime('%H:%M:%S'),'interval':'0s','on-event':event,'policy':'read,write,test'})
            schedulers.append((api,name))
            current = command(api, '/ip/firewall/raw/print')
            # Other settlements remove their owned rules concurrently. Never
            # use those ephemeral IDs as an insertion anchor. Owned rules only
            # drop traffic, so appending behind them cannot bypass this freeze.
            anchor = next((r for r in current if not str(r.get('comment','')).startswith('MAX-SETTLEMENT:')), None)
            for rid,s in sorted(hot.items()):
                if s['row']['nasipaddress'] != ip:
                    continue
                address = str(ipaddress.IPv4Address(s['row']['framedipaddress']))
                for field in ('src-address','dst-address'):
                    words = {'chain':'prerouting','action':'drop',field:address+'/32','comment':comment}
                    if anchor:
                        words['place-before'] = anchor['.id']
                    command(api, '/ip/firewall/raw/add', words)
                    installed.append((api,comment))
        # Require two identical counter observations after packet queues drain.
        previous = None
        stable = 0
        deadline = time.monotonic()+12
        while time.monotonic()<deadline:
            observed = {}
            for ip,api in apis.items():
                # One NAS read per service, not one full read for every session.
                views={kind:live_view(api,next(s for s in hot.values() if s['row']['nasipaddress']==ip and s.get('kind','hotspot')==kind))
                       for kind in {s.get('kind','hotspot') for s in hot.values() if s['row']['nasipaddress']==ip}}
                for rid,s in hot.items():
                    if s['row']['nasipaddress'] != ip:
                        continue
                    live=views[s.get('kind','hotspot')]
                    match=[r for r in live if identity(r)==original_identity(s) and r.get('.id')==s['live']['.id']]
                    if s.get('kind')=='pppoe' and match:
                        sid=lambda v:str(v).lower().removeprefix('0x')
                        if sid(match[0].get('session-id'))!=sid(s['row']['acctsessionid']):raise RuntimeError('PPP generation changed')
                    if len(match)!=1 or match[0].get('radius')!='true':
                        raise RuntimeError('NAS session identity changed while freezing')
                    r=match[0]
                    observed[rid]=dict(s,input=counters(r)[0],output=counters(r)[1],seconds=seconds(r['uptime']),evidence_token=token)
            values={rid:(s['input'],s['output']) for rid,s in observed.items()}
            stable = stable+1 if values==previous else 0
            previous=values
            if stable>=2:
                completed=observed
                break
            time.sleep(.25)
        if not completed:
            raise RuntimeError('NAS counters did not stabilize under the traffic freeze')
        proof=journal(completed,'frozen')
        for rid,s in completed.items():
            removing=True
            command(apis[s['row']['nasipaddress']],active_menu(s)+'/remove',{'numbers':s['live']['.id']})
        for ip,api in apis.items():
            views={kind:live_view(api,next(s for s in completed.values() if s['row']['nasipaddress']==ip and s.get('kind','hotspot')==kind))
                   for kind in {s.get('kind','hotspot') for s in completed.values() if s['row']['nasipaddress']==ip}}
            for s in completed.values():
                if s['row']['nasipaddress']==ip and any(identity(r)==original_identity(s) for r in views[s.get('kind','hotspot')]):
                    raise RuntimeError('NAS still has target sessions after disconnect')
        journal(completed,'detached')
        # Give already-emitted final packets a bounded chance to arrive. Hotspot
        # retains its verified NAS-counter fallback; no indefinite Stop barrier.
        stop_deadline=time.monotonic()+3
        while time.monotonic()<stop_deadline:
            if all(actual_stop(query_one('SELECT acctstoptime,acctterminatecause FROM radacct WHERE radacctid=%s',(rid,))) for rid in completed):
                break
            time.sleep(.1)
        seal_rows(completed)
        sealed=True
        proof.unlink()
        # Sealed sessions are already included in canonical accounting, not overlaid twice.
        for rid in completed:
            snapshot.pop(rid,None)
    except Exception as exc:
        logging.getLogger(__name__).warning('NAS settlement failed for %d sessions: %s: %s', len(hot), type(exc).__name__, str(exc))
        raise SettlementPending('تعذر تثبيت عدادات الجلسات وتأكيد فصلها؛ لم تُنفذ العملية ولم يُخصم الرصيد.') from exc
    finally:
        if proof is not None and not removing and proof.exists():
            proof.unlink()
        # After a partial/ambiguous disconnect keep the freeze proof intact for
        # recovery; the router watchdog bounds it even if this process dies.
        if removing and not sealed:
            installed=[];schedulers=[]
        for api,comment in installed:
            try:
                for row in command(api,'/ip/firewall/raw/print',['?comment='+comment]):
                    command(api,'/ip/firewall/raw/remove',{'numbers':row['.id']})
            except Exception:
                pass  # The NAS scheduler removes any remaining owned rules.
        for api,name in schedulers:
            try:
                # Keep the watchdog if removal could not be verified.
                comment='MAX-SETTLEMENT:'+token
                if not command(api,'/ip/firewall/raw/print',['?comment='+comment]):
                    for row in command(api,'/system/scheduler/print',['?name='+name]):
                        command(api,'/system/scheduler/remove',{'numbers':row['.id']})
            except Exception:
                pass
        for api in apis.values():
            api.close()
