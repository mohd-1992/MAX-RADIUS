"""Compact report-only counter deltas, atomic checkpoints and bounded scan cycles.

No raw-sample archive, router calls, or writes to accounting/financial records.
Quarter-hour summaries allow hour/day/month views and quarter-hour timezones.
Differences retain Interim resolution; missing periods are never invented zeros.
"""
import datetime as dt
import hashlib
import json
import threading
from zoneinfo import ZoneInfo
from services.report_time_axis import STORAGE, axis

MAX_INTERVAL_SECONDS=300
CHECKPOINT_HOURS=48


def session_key(row):
    return hashlib.sha256(json.dumps([str(row.get(k) or '') for k in
        ('acctuniqueid','username','acctsessionid','nasipaddress','acctstarttime')],ensure_ascii=False).encode()).hexdigest()


def counter(value):
    if value is None:return None
    value=int(value)
    return value if 0<=value<=9223372036854775807 else None


def delta(before,after,seconds,same_point=True):
    if not same_point or seconds<0:return None,None,1
    differences=[];gaps=0
    for name in ('download','upload'):
        a,b=before.get(name),after.get(name)
        valid=a is not None and b is not None and a>=0 and b>=a
        differences.append(int(b)-int(a) if valid else None)
        gaps+=not valid
    return *differences,int(gaps)


def capture_rows(conn,rows,monitor_started_at=None):
    """Caller commits once: summary increments and their checkpoint are inseparable."""
    from services.sales_source_service import packet_service
    from services.network_traffic_service import point_info
    changed=0
    rows=list(rows)
    known={}
    with conn.cursor() as cur:
        keys=[session_key(row) for row in rows if row.get('acctstarttime')]
        for offset in range(0,len(keys),500):
            batch=keys[offset:offset+500]
            cur.execute('SELECT * FROM wisp_traffic_checkpoints WHERE session_key IN ('+','.join('%s' for _ in batch)+')',tuple(batch))
            known.update((item['session_key'],item) for item in cur.fetchall())
        for row in rows:
            stamps=[t for t in (row.get('acctstoptime'),row.get('acctupdatetime'),row.get('acctstarttime')) if t]
            if not stamps or not row.get('acctstarttime'):continue
            observed=max(stamps);key=session_key(row)
            point=point_info(dict(nas_address=row.get('nasipaddress'),service_type=packet_service(row),
                server_name=row.get('calledstationid') or '',server_interface=row.get('nasportid') or ''),{})
            owner=int(row.get('reseller_id') or 0)
            after=dict(download=counter(row.get('acctoutputoctets')),upload=counter(row.get('acctinputoctets')))
            cached=known.get(key)
            compare_owner=cached['owner_scope'] if cached and row.get('owner_account_exists') is False and owner==0 else owner
            if cached and all((cached['observed_at']==observed,
                    cached['closed_at']==row.get('acctstoptime'),cached['download']==after['download'],
                    cached['upload']==after['upload'],cached['owner_scope']==compare_owner,cached['point_key']==point['server_key'])):
                continue
            if cached:
                inserted=False
            else:
                cur.execute('''INSERT IGNORE INTO wisp_traffic_checkpoints
                    (session_key,radacctid,observed_at,closed_at,download,upload,owner_scope,point_key)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',(key,int(row.get('radacctid') or 0),observed,row.get('acctstoptime'),after['download'],after['upload'],owner,point['server_key']))
                inserted=bool(cur.rowcount)
            if inserted:
                changed+=1
                # A newly started session has an accounting zero at Start.
                # Do not backfill sessions predating this monitoring generation.
                if monitor_started_at is None or row['acctstarttime']<monitor_started_at or observed<row['acctstarttime']:
                    continue
                before=dict(observed_at=row['acctstarttime'],download=0,upload=0,owner_scope=owner,point_key=point['server_key'],closed_at=None)
            else:
                cur.execute('SELECT * FROM wisp_traffic_checkpoints WHERE session_key=%s FOR UPDATE',(key,))
                before=cur.fetchone()
            if observed<before['observed_at']:continue
            if row.get('owner_account_exists') is False and owner==0:owner=before['owner_scope']
            untyped=point_info(dict(nas_address=row.get('nasipaddress'),server_name=row.get('calledstationid') or ''),{})
            refinement=(point['point_known'] and point['service_type']=='hotspot' and before['point_key']==untyped['server_key']
                        and before['owner_scope']==owner)
            if observed==before['observed_at']:
                advances=any(after[k] is not None and before[k] is not None and after[k]>before[k] for k in ('download','upload'))
                if not advances:
                    if refinement:cur.execute('UPDATE wisp_traffic_checkpoints SET point_key=%s WHERE session_key=%s',(point['server_key'],key));changed+=1
                    if row.get('acctstoptime') and not before['closed_at']:
                        cur.execute('UPDATE wisp_traffic_checkpoints SET closed_at=%s WHERE session_key=%s',(row['acctstoptime'],key))
                    continue
            # Deleted accounts keep the captured history scope, never become public reseller data.
            if row.get('owner_account_exists') is False and owner==0:owner=before['owner_scope']
            seconds=(observed-before['observed_at']).total_seconds()
            down,up,gaps=delta(before,after,seconds)
            bucket=(observed-dt.timedelta(microseconds=1)).replace(second=0,microsecond=0)
            bucket=bucket.replace(minute=bucket.minute//15*15)
            old_bucket=before['observed_at'].replace(minute=before['observed_at'].minute//15*15,second=0,microsecond=0)
            moved=(before['point_key']!=point['server_key'] and not refinement) or before['owner_scope']!=owner
            delayed=seconds>MAX_INTERVAL_SECONDS and bucket!=old_bucket
            if delayed or moved:
                from services.traffic_interval_service import save_interval
                destination=dict(point,server_name=row.get('calledstationid') or '',server_interface=(row.get('nasportid') or '') if point['service_type']=='pppoe' else '')
                if moved:
                    destination.update(server_key=hashlib.sha256(('unallocated:'+point['nas_address']).encode()).hexdigest(),service_type='',server_name='',server_label='غير محدد',server_interface='',point_known=False)
                save_interval(cur,key,before,observed,destination,owner if before['owner_scope']==owner else -1,down,up,'point_change' if moved else 'delayed_sample')
            else:
                cur.execute('''INSERT INTO wisp_traffic_rollups
                (bucket_at,owner_scope,point_key,nas_address,service_type,server_name,server_interface,point_known,
                 download_bytes,upload_bytes,download_known,upload_known,gaps,observations)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE download_bytes=download_bytes+VALUES(download_bytes),
                    upload_bytes=upload_bytes+VALUES(upload_bytes),download_known=download_known+VALUES(download_known),
                    upload_known=upload_known+VALUES(upload_known),gaps=gaps+VALUES(gaps),observations=observations+1''',
                (bucket,owner,point['server_key'],point['nas_address'],point['service_type'],row.get('calledstationid') or '',
                 (row.get('nasportid') or '') if point['service_type']=='pppoe' else '',int(point['point_known']),
                 down or 0,up or 0,int(down is not None),int(up is not None),gaps))
            cur.execute('''UPDATE wisp_traffic_checkpoints SET radacctid=%s,observed_at=%s,closed_at=%s,
                download=%s,upload=%s,owner_scope=%s,point_key=%s WHERE session_key=%s''',
                (int(row.get('radacctid') or 0),observed,row.get('acctstoptime'),after['download'],after['upload'],owner,point['server_key'],key))
            if not inserted:changed+=1
    return changed


def collect(conn,now=None,page_size=1000,max_pages=5):
    """Keyset cursor resumes overflow immediately; no permanently excluded tail."""
    from database.traffic_samples_schema import ensure_epoch
    from services.session_service_evidence import evidence_join,protocol_fields
    proven=protocol_fields('a')
    now=now or dt.datetime.now(STORAGE).replace(tzinfo=None)
    if now.tzinfo:now=now.astimezone(STORAGE).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(),':traffic-rollup'),0) held")
        if not cur.fetchone()['held']:return dict(changed=0,scanned=0,pending=True,locked=True)
    changed=scanned=0;pending=True
    try:
        ensure_epoch(conn);conn.commit()
        for _ in range(max_pages):
            with conn.cursor() as cur:
                cur.execute('SELECT cursor_id,cycle_rows,started_at FROM wisp_traffic_rollup_state WHERE id=1 FOR UPDATE')
                state=cur.fetchone()
                cur.execute(f'''SELECT a.radacctid,a.acctuniqueid,a.username,a.acctsessionid,
                    a.nasipaddress,a.acctstarttime,a.acctstoptime,a.acctupdatetime,
                    a.calledstationid,a.nasportid,{proven['framedprotocol']} framedprotocol,{proven['servicetype']} servicetype,
                    a.acctinputoctets,a.acctoutputoctets,
                    COALESCE(v.reseller_id,v2.reseller_id) reseller_id,
                    (v.id IS NOT NULL OR v2.id IS NOT NULL) owner_account_exists
                    FROM radacct a {evidence_join('a',original='a.username')} LEFT JOIN wisp_vouchers v ON v.id=a.account_voucher_id
                    LEFT JOIN wisp_vouchers v2 ON a.account_voucher_id IS NULL
                        AND v2.username=COALESCE(a.account_username,a.username)
                        AND BINARY v2.username=BINARY COALESCE(a.account_username,a.username)
                    WHERE a.radacctid>%s AND a.acctstarttime IS NOT NULL
                      AND GREATEST(COALESCE(a.acctupdatetime,a.acctstarttime),COALESCE(a.acctstoptime,a.acctstarttime))>=%s
                      AND GREATEST(COALESCE(a.acctupdatetime,a.acctstarttime),COALESCE(a.acctstoptime,a.acctstarttime))<=%s
                    ORDER BY a.radacctid LIMIT %s''',
                    (state['cursor_id'],now-dt.timedelta(hours=CHECKPOINT_HOURS),now+dt.timedelta(seconds=5),page_size))
                rows=list(cur.fetchall())
            for row in rows:row['owner_account_exists']=bool(row['owner_account_exists'])
            changed+=capture_rows(conn,rows,state['started_at']);scanned+=len(rows)
            pending=len(rows)==page_size
            with conn.cursor() as cur:
                if pending:
                    cur.execute('UPDATE wisp_traffic_rollup_state SET cursor_id=%s,cycle_rows=cycle_rows+%s WHERE id=1',(rows[-1]['radacctid'],len(rows)))
                else:
                    cur.execute('''UPDATE wisp_traffic_rollup_state SET cursor_id=0,cycle_rows=0,
                        last_cycle_rows=%s,last_completed_at=%s WHERE id=1''',(int(state['cycle_rows'])+len(rows),now))
            conn.commit()
            if not pending:break
        with conn.cursor() as cur:
            cur.execute('''DELETE c FROM wisp_traffic_checkpoints c LEFT JOIN radacct a ON a.radacctid=c.radacctid
                WHERE c.observed_at<%s AND (c.closed_at IS NOT NULL OR a.radacctid IS NULL)''',(now-dt.timedelta(hours=CHECKPOINT_HOURS),))
        conn.commit()
        return dict(changed=changed,scanned=scanned,pending=pending,locked=False)
    except Exception:
        conn.rollback();raise
    finally:
        with conn.cursor() as cur:cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(),':traffic-rollup'))")


def read_windows(query,windows,selected,owner=None,aliases=None):
    # Read-only validation also protects an already-running new Web after an old restore.
    status=query('''SELECT s.epoch,s.started_at,k.`value` marker FROM wisp_traffic_rollup_state s
        LEFT JOIN wisp_system_settings k ON k.`key`='traffic_rollup_epoch' WHERE s.id=1''')
    if not status or status[0]['epoch']!=status[0]['marker']:raise RuntimeError('Traffic summaries require initialization after restore')
    cases=[];boundaries=[]
    for i,(_,start,end) in enumerate(windows):
        cases.append(f'WHEN bucket_at>=%s AND bucket_at<%s THEN {i}')
        boundaries.extend((start.astimezone(STORAGE).replace(tzinfo=None),end.astimezone(STORAGE).replace(tzinfo=None)))
    clauses=['bucket_at>=%s','bucket_at<%s'];params=[boundaries[0],boundaries[-1]]
    from services.traffic_interval_service import filters
    if aliases is None:
        from services.traffic_point_resolution_service import load_aliases
        aliases=load_aliases(query,owner,selected.get('nas')) if any(selected.get(k) not in (None,'','all') for k in ('server','service_type')) else {}
    scoped,values=filters(selected,owner,aliases=aliases);clauses+=scoped;params+=values
    sql='CASE '+' '.join(cases)+' END'
    rows=query(f'''SELECT {sql} bucket,
        SUM(CASE WHEN download_known>0 THEN download_bytes END) download,
        SUM(CASE WHEN upload_known>0 THEN upload_bytes END) upload,
        SUM(gaps) gaps,SUM(observations) observations
        FROM wisp_traffic_rollups WHERE {' AND '.join(clauses)} GROUP BY bucket''',tuple(boundaries+params))
    indexed={int(r['bucket']):dict(r) for r in rows if r['bucket'] is not None}
    from services.traffic_interval_service import read_intervals,contribution
    intervals=read_intervals(query,boundaries[0],boundaries[-1],selected,owner,aliases)
    result=[]
    for i,(_,first,last) in enumerate(windows):
        start=first.astimezone(STORAGE).replace(tzinfo=None);end=last.astimezone(STORAGE).replace(tzinfo=None)
        row=indexed.get(i,dict(download=None,upload=None,gaps=0,observations=0))
        row['gaps']=int(row.get('gaps') or 0);row['observations']=int(row.get('observations') or 0)
        for interval in intervals:
            if interval['end_at']<=start or interval['start_at']>=end:continue
            values=contribution(interval,start,end)
            for field in ('download','upload'):
                if values[field] is not None:row[field]=int(row.get(field) or 0)+int(values[field])
            row['gaps']+=int(any(value is None for value in values.values()));row['observations']+=1
        started=status[0].get('started_at')
        if started and start<started:row['gaps']+=1
        result.append(row)
    return result


def series(query,selected,owner=None,now=None,timezone='Asia/Aden'):
    from services.traffic_point_resolution_service import load_aliases
    aliases=load_aliases(query,owner,selected.get('nas')) if any(selected.get(k) not in (None,'','all') for k in ('server','service_type')) else {}
    extent=(None,None)
    if selected['period']=='all' or selected['period']=='custom' and not selected.get('date_from'):
        from services.traffic_interval_service import filters
        clauses,params=filters(selected,owner,aliases=aliases);scope=' WHERE '+' AND '.join(clauses) if clauses else ''
        edges=query('''SELECT MIN(first_seen) first_seen,MAX(last_seen) last_seen FROM (
            SELECT MIN(bucket_at) first_seen,MAX(bucket_at) last_seen FROM wisp_traffic_rollups'''+scope+'''
            UNION ALL SELECT MIN(start_at),MAX(end_at) FROM wisp_traffic_intervals'''+scope+') extents',tuple(params+params))[0]
        extent=(edges['first_seen'],edges['last_seen'])
    mode,windows,clock=axis(selected,now,timezone,extent)
    rows=read_windows(query,windows,selected,owner,aliases)
    return dict(labels=[w[0] for w in windows],
        download=[None if r['download'] is None else int(r['download'])/1073741824 for r in rows],
        upload=[None if r['upload'] is None else int(r['upload'])/1073741824 for r in rows],
        unknown_counters=sum(int(r['gaps'] or 0) for r in rows),observations=sum(int(r['observations'] or 0) for r in rows),
        granularity=mode,basis='counter_deltas')


def daily_rows(query,days=7,owner=None,now=None,timezone='Asia/Aden'):
    clock=(now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(timezone))
    dates=[clock.date()-dt.timedelta(days=i) for i in range(days-1,-1,-1)]
    windows=[(date.isoformat(),dt.datetime.combine(date,dt.time.min,ZoneInfo(timezone)),
        dt.datetime.combine(date+dt.timedelta(days=1),dt.time.min,ZoneInfo(timezone))) for date in dates]
    rows=read_windows(query,windows,{},owner)
    return [dict(date=date.isoformat(),download=r['download'],upload=r['upload'],
        unknown_download=int(r['gaps'] or r['download'] is None),unknown_upload=int(r['gaps'] or r['upload'] is None),
        sessions=None) for date,r in zip(dates,rows)]


_started=False


def start_worker():
    global _started
    if _started:return
    _started=True
    def run():
        import logging
        from database.db import get_connection,is_import_maintenance_active
        from services.account_lifecycle_service import job_lock
        import time
        wait=threading.Event();delay=5;metadata_due=0
        while not wait.wait(delay):
            delay=60
            try:
                if is_import_maintenance_active():continue
                if time.monotonic()>=metadata_due:
                    metadata_due=time.monotonic()+60
                    try:
                        from services.session_service_evidence import refresh
                        refresh()
                    except Exception:
                        logging.getLogger(__name__).warning('Session service evidence unavailable; accounting is unchanged.')
                with job_lock('factory-reset') as idle:
                    if idle:
                        with get_connection() as conn:result=collect(conn)
                        if result['pending'] and not result['locked']:delay=.1
                    else:delay=1
            except Exception:
                logging.getLogger(__name__).warning('Traffic rollup deferred; accounting is unchanged.')
    threading.Thread(target=run,name='traffic-rollups',daemon=True).start()
