"""Read NAS session identity; persist protocol evidence, never counters or Stops."""
import datetime as dt
import re
import time

_nas_cursor=0


def uptime_seconds(value):
    value=str(value or '')
    if ':' in value:
        match=re.fullmatch(r'(?:(\d+)d)?(\d+):(\d+):(\d+)',value)
        if not match:return None
        days,hours,minutes,seconds=match.groups()
        return int(days or 0)*86400+int(hours)*3600+int(minutes)*60+int(seconds)
    parts=re.findall(r'(\d+)([wdhms])',value)
    if ''.join(n+u for n,u in parts)!=value:return None
    return sum(int(n)*{'w':604800,'d':86400,'h':3600,'m':60,'s':1}[u] for n,u in parts) if parts else None


def matched_service(row,hotspot,pppoe,now):
    if row.get('acctstoptime') is not None or not row.get('acctstarttime'):return ''
    observed=row.get('acctupdatetime') or row['acctstarttime']
    if not 0<=(now-observed).total_seconds()<=360:return ''
    username=row.get('original_username') or row.get('username')
    mac=lambda value:re.sub('[^0-9A-F]','',str(value or '').upper())
    if not username or not row.get('framedipaddress') or not mac(row.get('callingstationid')):return ''
    found=[]
    for kind,rows in [('hotspot',hotspot),('pppoe',pppoe)]:
        for live in rows:
            if live.get('radius')!='true' or live.get('user' if kind=='hotspot' else 'name')!=username:continue
            if live.get('address')!=row['framedipaddress']:continue
            if mac(live.get('mac-address' if kind=='hotspot' else 'caller-id'))!=mac(row['callingstationid']):continue
            seconds=uptime_seconds(live.get('uptime'))
            if seconds is None or abs((now-row['acctstarttime']).total_seconds()-seconds)>15:continue
            if kind=='pppoe' and str(live.get('session-id','')).removeprefix('0x').lower()!=str(row.get('acctsessionid') or '').removeprefix('0x').lower():continue
            found.append(kind)
    return found[0] if len(found)==1 else ''


def evidence_join(table,alias='service_evidence',original=None):
    original=original or table+'.original_username'
    return f'''LEFT JOIN wisp_session_service_evidence {alias} ON {alias}.acctuniqueid=CONVERT({table}.acctuniqueid USING ascii)
        AND {alias}.nasipaddress={table}.nasipaddress AND {alias}.acctsessionid={table}.acctsessionid
        AND {alias}.acctstarttime={table}.acctstarttime AND BINARY {alias}.original_username=BINARY {original}'''


def protocol_fields(table,alias='service_evidence'):
    protocol=f"CASE WHEN COALESCE(TRIM({table}.framedprotocol),'')='' AND {alias}.service_type='pppoe' THEN 'PPP' ELSE {table}.framedprotocol END"
    service=f"CASE WHEN COALESCE(TRIM({table}.servicetype),'')='' AND {alias}.service_type='hotspot' THEN 'Login-User' ELSE {table}.servicetype END"
    return dict(framedprotocol=protocol,servicetype=service)


def apply_evidence(row,evidence):
    from services.sales_source_service import packet_service
    if not evidence or evidence.get('service_type') not in ('hotspot','pppoe'):return row
    original=row.get('original_username') or row.get('username')
    if evidence.get('original_username')!=original:return row
    if any(row.get(key)!=evidence.get(key) for key in ('acctuniqueid','nasipaddress','acctsessionid','acctstarttime')):return row
    kind=evidence['service_type'];recorded=packet_service(row)
    if recorded and recorded!=kind:return row
    result=dict(row)
    if kind=='hotspot' and not result.get('servicetype'):result['servicetype']='Login-User'
    if kind=='pppoe' and not result.get('framedprotocol'):result['framedprotocol']='PPP'
    return result


def record_evidence(conn,row,kind,now):
    if kind not in ('hotspot','pppoe') or not re.fullmatch('[0-9a-fA-F]{32}',str(row.get('acctuniqueid') or '')):return False
    original=row.get('original_username') or row.get('username')
    if not original or not row.get('acctstarttime') or not row.get('acctsessionid'):return False
    from services.sales_source_service import packet_service
    raw_kind=packet_service(row)
    if raw_kind and raw_kind!=kind:return False
    key=(row['acctuniqueid'],row['nasipaddress'],row['acctsessionid'],row['acctstarttime'])
    with conn.cursor() as cur:
        cur.execute('SELECT service_type,original_username,source FROM wisp_session_service_evidence WHERE acctuniqueid=%s AND nasipaddress=%s AND acctsessionid=%s AND acctstarttime=%s FOR UPDATE',key)
        previous=cur.fetchone()
        if previous:
            if previous['service_type']==kind and previous['original_username']==original:return True
            cur.execute("UPDATE wisp_session_service_evidence SET service_type='',source='conflicting_evidence',observed_at=%s WHERE acctuniqueid=%s AND nasipaddress=%s AND acctsessionid=%s AND acctstarttime=%s",(now,*key))
            return False
        cur.execute('''INSERT INTO wisp_session_service_evidence
            (acctuniqueid,nasipaddress,acctsessionid,acctstarttime,original_username,service_type,observed_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',(*key,original,kind,now))
    return True


def refresh(limit=500,max_nas=2,api_factory=None):
    global _nas_cursor
    from database.db import get_connection
    from services.account_lifecycle_service import job_lock
    if api_factory is None:
        from core.mikrotik_api import RouterOSApiProtocol
        api_factory=RouterOSApiProtocol
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT CURRENT_TIMESTAMP now');now=cur.fetchone()['now']
            cur.execute('''SELECT a.* FROM radacct a '''+evidence_join('a',original='a.username')+'''
                WHERE a.acctstoptime IS NULL AND COALESCE(a.framedprotocol,'')='' AND COALESCE(a.servicetype,'')=''
                AND service_evidence.acctuniqueid IS NULL AND COALESCE(a.acctupdatetime,a.acctstarttime)>=CURRENT_TIMESTAMP-INTERVAL 6 MINUTE
                ORDER BY a.acctupdatetime DESC LIMIT %s''',(max(1,min(1000,int(limit))),))
            rows=list(cur.fetchall());addresses=sorted({row['nasipaddress'] for row in rows})
            if addresses:
                offset=_nas_cursor%len(addresses)
                addresses=(addresses[offset:]+addresses[:offset])[:max(1,int(max_nas))]
                _nas_cursor=(offset+len(addresses))%len({row['nasipaddress'] for row in rows})
            devices=[]
            for address in addresses:
                cur.execute('SELECT * FROM wisp_nas_devices WHERE ip_address=%s LIMIT 1',(address,));device=cur.fetchone()
                if device and device.get('api_username'):devices.append(device)
        conn.rollback()
    verified=[];deadline=time.monotonic()+8
    for device in devices:
        if time.monotonic()>=deadline:break
        api=api_factory(device['ip_address'],port=int(device.get('api_port') or 8728),timeout=1.5)
        try:
            api.connect()
            if not api.login(device['api_username'],device.get('api_password') or ''):continue
            hot=api.execute_command('/ip/hotspot/active/print',['=.proplist=user,address,mac-address,uptime,radius'])
            ppp=api.execute_command('/ppp/active/print',['=.proplist=name,address,caller-id,uptime,session-id,radius'])
            for row in rows:
                if row['nasipaddress']!=device['ip_address']:continue
                kind=matched_service(row,hot,ppp,now)
                if kind:verified.append((row,kind))
        except Exception:
            pass  # Missing evidence remains unknown; never alter accounting on API failure.
        finally:api.close()
    saved=0
    with job_lock('factory-reset') as idle:
        if not idle:return 0
        with get_connection() as conn:
            try:
                for row,kind in verified:
                    with conn.cursor() as cur:
                        cur.execute('SELECT * FROM radacct WHERE radacctid=%s FOR UPDATE',(row['radacctid'],));current=cur.fetchone()
                    fields=('acctuniqueid','nasipaddress','acctsessionid','acctstarttime','username')
                    if current and current['acctstoptime'] is None and all(current[k]==row[k] for k in fields):saved+=int(record_evidence(conn,current,kind,now))
                conn.commit()
            except Exception:conn.rollback();raise
    return saved
