"""Immutable sale-point evidence; never infer a sale from a later login."""
import hashlib
import ipaddress
import json

RESOLUTION_VERSION = 2
SOURCE_FIELDS = ('server_key','nas_address','nas_label','service_type','server_name',
                 'server_interface','evidence_uid','attribution','service_attribution',
                 'resolution_status','resolution_reason')


def packet_service(row):
    ppp = str(row.get('framedprotocol') or '').strip().lower() == 'ppp'
    hotspot = str(row.get('servicetype') or '').strip().lower() in ('login-user','login')
    if ppp and hotspot:
        return 'conflict'
    return 'pppoe' if ppp else 'hotspot' if hotspot else ''


def resolve_historical_source(existing, rows, hint_kind=''):
    """Separate packet proof from a present-day package reference; never guess a server from a later login."""
    old = {k:existing.get(k) or '' for k in SOURCE_FIELDS}
    old['server_key'] = old['server_key'] or 'unknown'
    if old['server_key']!='unknown' and old['resolution_status']!='inferred':
        return dict(old,service_attribution=old['service_attribution'] or 'recorded_snapshot',
                    resolution_status='confirmed',resolution_reason='')
    eligible=[]
    for row in rows:
        if row.get('acctterminatecause')=='Consolidated-Historical-Import':continue
        try:
            address=ipaddress.ip_address(str(row.get('nasipaddress') or ''))
            if address.is_loopback or address.is_unspecified:continue
        except ValueError:
            continue
        eligible.append(row)
    kinds={packet_service(r) for r in eligible}-{''}
    if old['resolution_status']=='inferred' and old['service_attribution']=='package_reference':
        # The reference captured on the first review is historical metadata now;
        # editing today's package must not rewrite that inference on every retry.
        hint_kind=old['service_type']
        if old['server_key']!='unknown' and not eligible:
            return dict(old,resolution_reason='evidence_no_longer_available')
    recorded = old['service_type'] if old['service_attribution']!='package_reference' else ''
    conflict='conflict' in kinds or len(kinds)>1 or bool(recorded and kinds and recorded not in kinds)
    if conflict:
        return dict(old,server_key='unknown',nas_address='',nas_label='',server_name='',server_interface='',
                    evidence_uid='',attribution='unknown',resolution_status='ambiguous',
                    resolution_reason='conflicting_service_evidence')
    kind=recorded or (next(iter(kinds)) if kinds else hint_kind if hint_kind in ('hotspot','pppoe') else '')
    kind_source=(old['service_attribution'] or 'recorded_snapshot') if recorded else 'packet_protocol' if kinds else 'package_reference' if kind else ''
    point=source_from_rows(eligible,kind,'historical_interval')
    point['service_type']=kind
    point['nas_label']=''
    point['service_attribution']=kind_source
    # A missing internal-server name does not erase an independently known NAS.
    addresses={str(ipaddress.ip_address(str(r['nasipaddress']))) for r in eligible}
    if point['server_key']=='unknown' and len(addresses)==1:
        point['nas_address']=next(iter(addresses))
    point['resolution_status']=('inferred' if kind_source=='package_reference' else
                                'confirmed' if point['server_key']!='unknown' else 'partial' if kind or addresses else 'unknown')
    point['resolution_reason']=('package_reference' if kind_source=='package_reference' else '')
    if not eligible:
        point['resolution_reason']='no_matching_session'
    elif point['server_key']=='unknown':
        keys={server_key(str(r['nasipaddress']),kind,str(r.get('calledstationid') or ''),str(r.get('nasportid') or '')) for r in eligible}
        point['resolution_reason']='multiple_session_points' if len(keys-{'unknown'})>1 else 'incomplete_session_fields'
        if len(keys-{'unknown'})>1:point['resolution_status']='ambiguous'
    if point['server_key']!='unknown':
        point['evidence_uid']=min(str(r.get('acctuniqueid') or '') for r in eligible)
    return point


def server_key(nas, kind, server, interface=''):
    if not nas or kind not in ('hotspot', 'pppoe') or not server or (kind == 'pppoe' and not interface):
        return 'unknown'
    fields = (nas, kind, server, interface if kind == 'pppoe' else '')
    return hashlib.sha256(''.join(str(len(v))+':'+v for v in fields).encode()).hexdigest()


def source_from_rows(rows, kind, provenance='accounting_open'):
    points = {}
    invalid = False
    for row in rows:
        nas = str(row.get('nasipaddress') or '').strip()
        name = str(row.get('calledstationid') or '').strip()
        interface = str(row.get('nasportid') or '').strip()
        try:
            address = ipaddress.ip_address(nas)
            if address.is_loopback or address.is_unspecified:
                invalid = True
                continue
            nas = str(address)
        except ValueError:
            invalid = True
            continue
        if row.get('acctterminatecause') == 'Consolidated-Historical-Import':
            invalid = True
            continue
        key = server_key(nas, kind, name, interface)
        if key == 'unknown' or any(ord(c) < 32 or ord(c)==127 for c in name + interface):
            invalid = True
            continue
        points[key] = dict(server_key=key, nas_address=nas, service_type=kind,
                           server_name=name, server_interface=interface if kind == 'pppoe' else '',
                           evidence_uid=str(row.get('acctuniqueid') or ''), attribution=provenance)
    if invalid or len(points) != 1:
        return dict(server_key='unknown', nas_address='', service_type=kind if kind in ('hotspot', 'pppoe') else '',
                    server_name='', server_interface='', evidence_uid='', attribution='unknown')
    return next(iter(points.values()))


def record_sale_source(conn, event_kind, event_id, username):
    """Call in the financial transaction, immediately after the ledger insert."""
    from database.db import adapt_query
    if event_kind not in ('voucher', 'invoice'):
        raise ValueError('Invalid sale event kind')
    with conn.cursor() as cur:
        if event_kind == 'voucher':
            cur.execute(adapt_query('''SELECT p.service_type FROM wisp_voucher_sales s
                JOIN wisp_vouchers v ON v.id=s.voucher_id LEFT JOIN wisp_packages p ON p.id=v.package_id WHERE s.id=?''', conn), (event_id,))
        else:
            cur.execute(adapt_query('''SELECT s.service_type FROM wisp_invoices i
                JOIN wisp_subscribers s ON s.id=i.subscriber_id WHERE i.id=?''', conn), (event_id,))
        account = cur.fetchone() or {}
        kind = account.get('service_type') or ''
        kind_source='operation_snapshot' if kind in ('hotspot','pppoe') else ''
        conflicting=False
        from services.online_policy_service import local
        snapshot = getattr(local, 'snapshot', None)
        if snapshot is not None:
            verified = [item for item in (snapshot.values() if isinstance(snapshot,dict) else ())
                        if (item.get('row', {}).get('account_username') or item.get('row', {}).get('username')) == username]
            kinds = {item.get('kind') for item in verified}
            if kinds == {'pppoe'}:
                kind = 'pppoe'
                kind_source='verified_session'
            elif kinds == {'hotspot'}:
                kind = 'hotspot'
                kind_source='verified_session'
            elif len(kinds)>1:
                kind='';kind_source='';conflicting=True
            candidates = [item['row'] for item in verified]
            point = source_from_rows(candidates, kind, 'verified_preoperation_session')
        else:
            cur.execute(adapt_query('''SELECT nasipaddress,calledstationid,nasportid,acctuniqueid,acctterminatecause,framedprotocol,servicetype
                FROM radacct_owned WHERE username=? AND acctstoptime IS NULL
                AND acctstarttime<=NOW() AND COALESCE(acctupdatetime,acctstarttime)>=NOW()-INTERVAL 3 MINUTE''', conn), (username,))
            candidates=list(cur.fetchall())
            kinds={packet_service(row) for row in candidates}-{''}
            if 'conflict' in kinds or len(kinds)>1:
                kind='';kind_source='';conflicting=True
            elif kinds:
                kind=next(iter(kinds));kind_source='packet_protocol'
            point = source_from_rows(candidates, kind)
        cur.execute(adapt_query('''SELECT shortname FROM nas WHERE nasname=? ORDER BY id LIMIT 1''', conn), (point['nas_address'],))
        point['nas_label'] = (cur.fetchone() or {}).get('shortname') or point['nas_address']
        point['service_attribution']=kind_source if point['service_type'] else ''
        point['resolution_status']='ambiguous' if conflicting else 'confirmed' if point['server_key']!='unknown' else 'partial' if point['service_type'] else 'unknown'
        point['resolution_reason']='conflicting_service_evidence' if conflicting else ''
        columns = ('server_key','nas_address','nas_label','service_type','server_name','server_interface','evidence_uid','attribution','service_attribution','resolution_status','resolution_reason')
        cur.execute(adapt_query('''INSERT INTO wisp_sale_sources
            (event_kind,event_id,server_key,nas_address,nas_label,service_type,server_name,server_interface,evidence_uid,attribution,service_attribution,resolution_status,resolution_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE event_id=event_id''', conn), (event_kind, event_id, *(point[k] for k in columns)))


def backfill_sale_sources(conn):
    """Bounded historical snapshots; protocol must be evidenced by old packets."""
    from database.db import adapt_query
    for event_kind in ('voucher', 'invoice'):
        last_id = 0
        while True:
            with conn.cursor() as cur:
                if event_kind == 'voucher':
                    sql = """SELECT s.id,s.username,s.activated_at AS event_at
                        FROM wisp_voucher_sales s LEFT JOIN wisp_sale_sources x
                        ON x.event_kind='voucher' AND x.event_id=s.id
                        WHERE x.event_id IS NULL AND s.id>%s ORDER BY s.id LIMIT 500"""
                else:
                    sql = """SELECT i.id,s.username,i.paid_at AS event_at
                        FROM wisp_invoices i LEFT JOIN wisp_sale_sources x
                        ON x.event_kind='invoice' AND x.event_id=i.id
                        LEFT JOIN wisp_subscribers s ON s.id=i.subscriber_id
                        WHERE i.status='paid' AND x.event_id IS NULL AND i.id>%s ORDER BY i.id LIMIT 500"""
                cur.execute(sql, (last_id,))
                events = cur.fetchall()
            if not events:
                break
            for event in events:
                candidates = []
                with conn.cursor() as cur:
                    if event.get('event_at') and event.get('username'):
                        for table in ('radacct_owned', 'radacct_archive_owned', 'wisp_imported_session_history_owned', 'wisp_traffic_history_owned'):
                            cur.execute(adapt_query(f"""SELECT nasipaddress,calledstationid,nasportid,acctuniqueid,
                                acctterminatecause,framedprotocol,servicetype FROM {table}
                                WHERE username=? AND acctstarttime<=?
                                AND COALESCE(acctstoptime,acctupdatetime)>=?""", conn),
                                (event['username'], event['event_at'], event['event_at']))
                            candidates.extend(cur.fetchall())
                    kinds = {('pppoe' if str(r.get('framedprotocol') or '').lower()=='ppp'
                              else 'hotspot' if str(r.get('servicetype') or '').lower() in ('login-user','login') else '')
                             for r in candidates}
                    kind = next(iter(kinds)) if len(kinds)==1 else ''
                    point = source_from_rows(candidates, kind, 'historical_interval')
                    cur.execute(adapt_query('SELECT shortname FROM nas WHERE nasname=? ORDER BY id LIMIT 1', conn), (point['nas_address'],))
                    label = (cur.fetchone() or {}).get('shortname') or point['nas_address']
                    cur.execute(adapt_query("""INSERT INTO wisp_sale_sources
                        (event_kind,event_id,server_key,nas_address,nas_label,service_type,server_name,server_interface,evidence_uid,attribution)
                        VALUES (?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE event_id=event_id""", conn),
                        (event_kind,event['id'],point['server_key'],point['nas_address'],label,point['service_type'],point['server_name'],point['server_interface'],point['evidence_uid'],point['attribution']))
            conn.commit()
            last_id = events[-1]['id']


def enrich_sale_sources(conn, limit=250, retry_seconds=3600):
    """One bounded, audited metadata batch; no ledger, wallet, contract or accounting writes."""
    limit=max(1,min(500,int(limit)))
    counts=dict(scanned=0,changed=0,confirmed=0,inferred=0,partial=0,ambiguous=0,unknown=0)
    with conn.cursor() as cur:
        cur.execute('SELECT DATABASE() AS name')
        lock='max-sale-source-enrichment:'+cur.fetchone()['name']
        cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock,))
        if cur.fetchone()['acquired']!=1:return dict(counts,busy=True)
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT x.*,e.account_id,e.username,e.event_at,e.package_name,
                    e.current_username,e.current_package,e.hint_kind,e.identity_conflict
                FROM wisp_sale_sources x JOIN (
                SELECT 'voucher' AS event_kind,s.id AS event_id,s.voucher_id AS account_id,
                    s.username,s.activated_at AS event_at,s.package_name,v.username AS current_username,
                    p.name AS current_package,p.service_type AS hint_kind,
                    CASE WHEN v.id IS NOT NULL AND COALESCE(s.serial_number,'')<>''
                      AND COALESCE(v.serial_number,'')<>'' AND BINARY s.serial_number<>BINARY v.serial_number THEN 1 ELSE 0 END AS identity_conflict
                FROM wisp_voucher_sales s LEFT JOIN wisp_vouchers v ON v.id=s.voucher_id LEFT JOIN wisp_packages p ON p.id=v.package_id
                UNION ALL SELECT 'invoice',i.id,i.subscriber_id,s.username,i.paid_at,i.package_name,
                    s.username,p.name,p.service_type,0
                FROM wisp_invoices i LEFT JOIN wisp_subscribers s ON s.id=i.subscriber_id
                    LEFT JOIN wisp_packages p ON p.id=s.package_id WHERE i.status='paid'
                ) e ON e.event_kind=x.event_kind AND e.event_id=x.event_id
                WHERE x.resolution_version<%s OR ((x.server_key='unknown' OR x.resolution_status='inferred')
                    AND (x.reviewed_at IS NULL OR x.reviewed_at<=NOW()-INTERVAL %s SECOND))
                ORDER BY x.reviewed_at,x.event_kind,x.event_id LIMIT %s''',(RESOLUTION_VERSION,int(retry_seconds),limit))
            events=list(cur.fetchall())
            cur.execute('SELECT name,service_type FROM wisp_packages')
            packages={}
            for item in cur.fetchall():packages.setdefault(item['name'],set()).add(item['service_type'])
            for event in events:
                rows=[]
                if event['event_at'] and not event['identity_conflict']:
                    owner='voucher' if event['event_kind']=='voucher' else 'subscriber'
                    other='subscriber' if owner=='voucher' else 'voucher'
                    names={v for v in (event['username'],event['current_username']) if v}
                    if event['account_id']:
                        cur.execute(f'SELECT username FROM wisp_{owner}_names WHERE {owner}_id=%s',(event['account_id'],))
                        names.update(r['username'] for r in cur.fetchall())
                    if names or event['account_id']:
                        placeholders=','.join(['%s']*len(names)) or 'NULL'
                        for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
                            cur.execute(f'''SELECT nasipaddress,calledstationid,nasportid,acctuniqueid,
                                acctterminatecause,framedprotocol,servicetype FROM {table}
                                WHERE (account_{owner}_id=%s OR (account_{owner}_id IS NULL
                                  AND username IN ({placeholders})))
                                  AND account_{other}_id IS NULL
                                  AND acctstarttime<=%s AND COALESCE(acctstoptime,acctupdatetime)>= %s''',
                                (event['account_id'],*sorted(names),event['event_at'],event['event_at']))
                            rows.extend(cur.fetchall())
                hint=event['hint_kind'] if event['package_name']==event['current_package'] else ''
                if not hint and len(packages.get(event['package_name'],set()))==1:
                    hint=next(iter(packages[event['package_name']]))
                cur.execute('SELECT * FROM wisp_sale_sources WHERE event_kind=%s AND event_id=%s FOR UPDATE',
                            (event['event_kind'],event['event_id']))
                before=cur.fetchone()
                result=resolve_historical_source(before,rows,hint)
                if event['identity_conflict'] and before['server_key']=='unknown':
                    result.update(server_key='unknown',nas_address='',nas_label='',server_name='',server_interface='',
                                  evidence_uid='',attribution='unknown',resolution_status='ambiguous',resolution_reason='account_identity_conflict')
                if result['nas_address'] and not result['nas_label']:
                    cur.execute('SELECT shortname FROM nas WHERE nasname=%s ORDER BY id LIMIT 1',(result['nas_address'],))
                    result['nas_label']=(cur.fetchone() or {}).get('shortname') or result['nas_address']
                old={k:before.get(k) or '' for k in SOURCE_FIELDS}
                if old!=result:
                    previous=json.dumps(old,sort_keys=True,ensure_ascii=False)
                    updated=json.dumps(result,sort_keys=True,ensure_ascii=False)
                    digest=hashlib.sha256((previous+'\n'+updated).encode()).hexdigest()
                    cur.execute('''INSERT INTO wisp_sale_source_audit
                        (event_kind,event_id,evidence_hash,before_json,after_json) VALUES(%s,%s,%s,%s,%s)''',
                        (event['event_kind'],event['event_id'],digest,previous,updated))
                    cur.execute('UPDATE wisp_sale_sources SET '+','.join(k+'=%s' for k in SOURCE_FIELDS)+
                                ',resolution_version=%s,reviewed_at=NOW() WHERE event_kind=%s AND event_id=%s',
                                (*[result[k] for k in SOURCE_FIELDS],RESOLUTION_VERSION,event['event_kind'],event['event_id']))
                    counts['changed']+=1
                else:
                    cur.execute('UPDATE wisp_sale_sources SET resolution_version=%s,reviewed_at=NOW() WHERE event_kind=%s AND event_id=%s',
                                (RESOLUTION_VERSION,event['event_kind'],event['event_id']))
                counts['scanned']+=1
                counts[result['resolution_status']]+=1
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


_enrichment_started=False


def start_sales_source_enrichment_worker():
    """Resume after restart, retry unresolved evidence hourly, and leave financial operations independent."""
    import threading
    global _enrichment_started
    if _enrichment_started:return
    _enrichment_started=True
    def run():
        import logging
        from database.db import get_connection,is_import_maintenance_active
        from services.account_lifecycle_service import job_lock
        wait=threading.Event()
        wait.wait(5)
        while True:
            try:
                if is_import_maintenance_active():
                    wait.wait(60)
                    continue
                with job_lock('factory-reset') as reset_idle:
                    if not reset_idle:
                        wait.wait(60)
                        continue
                    with get_connection() as conn:result=enrich_sale_sources(conn)
                wait.wait(.1 if result['scanned']==250 else 60)
            except Exception:
                logging.getLogger(__name__).warning('Sale source enrichment deferred; financial operations are unchanged.')
                wait.wait(60)
    threading.Thread(target=run,name='sale-source-enrichment',daemon=True).start()
