"""Period consumption over the same measured sources used by the dashboard."""
import datetime as dt
from zoneinfo import ZoneInfo
from services.traffic_interval_service import filters,read_intervals,contribution
from services.sales_reporting_service import date_bounds
from services.report_time_axis import STORAGE


def read_points(query,start,end,selected,owner,aliases=None):
    from services.traffic_point_resolution_service import load_aliases,merge_points
    if aliases is None:aliases=load_aliases(query,owner,selected.get('nas'))
    clauses=['bucket_at>=%s','bucket_at<%s'];params=[start,end]
    scoped,values=filters(selected,owner,aliases=aliases);clauses+=scoped;params+=values
    rows=query('''SELECT point_key AS server_key,nas_address,service_type,server_name,server_interface,
        MIN(point_known) point_known,SUM(observations) sessions,SUM(gaps) gaps,
        SUM(CASE WHEN download_known>0 THEN download_bytes END) download,
        SUM(CASE WHEN upload_known>0 THEN upload_bytes END) upload,
        SUM(GREATEST(0,observations-download_known)) unknown_download,
        SUM(GREATEST(0,observations-upload_known)) unknown_upload,MAX(bucket_at) last_observed
        FROM wisp_traffic_rollups WHERE '''+' AND '.join(clauses)+'''
        GROUP BY point_key,nas_address,service_type,server_name,server_interface''',tuple(params))
    indexed={row['server_key']:dict(row) for row in rows}
    for interval in read_intervals(query,start,end,selected,owner,aliases):
        point=indexed.setdefault(interval['point_key'],dict(server_key=interval['point_key'],nas_address=interval['nas_address'],
            service_type=interval['service_type'],server_name=interval['server_name'],server_interface=interval['server_interface'],
            point_known=interval['point_known'],sessions=0,gaps=0,download=None,upload=None,unknown_download=0,unknown_upload=0,last_observed=None))
        values=contribution(interval,start,end);point['sessions']=int(point['sessions'] or 0)+1
        for field in ('download','upload'):
            if values[field] is None:point['unknown_'+field]=int(point['unknown_'+field] or 0)+1
            else:point[field]=int(point[field] or 0)+int(values[field])
        point['last_observed']=max(point.get('last_observed') or interval['end_at'],interval['end_at'])
    return merge_points(indexed.values(),aliases)


def report(selected,now,timezone,owner):
    from database.db import get_connection
    from services.network_traffic_service import sum_rows,SORT_KEYS
    from services.session_display_service import connection_point_label
    from services.report_navigation_service import navigation
    clock=(now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(timezone))
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                def query(sql,params=()):cur.execute(sql,params);return list(cur.fetchall())
                state=query('''SELECT s.epoch,s.started_at,k.`value` marker FROM wisp_traffic_rollup_state s
                    LEFT JOIN wisp_system_settings k ON k.`key`='traffic_rollup_epoch' WHERE s.id=1''')
                if not state or state[0]['epoch']!=state[0]['marker']:raise RuntimeError('Measurement history requires initialization')
                start,end=date_bounds(selected['period'],selected['date_from'],selected['date_to'],now,timezone)
                scope='' if owner is None else ' WHERE owner_scope=%s';params=() if owner is None else (int(owner),)
                bounds=query('''SELECT MIN(first_seen) first_seen,MAX(last_seen) last_seen FROM (
                    SELECT MIN(bucket_at) first_seen,MAX(bucket_at) last_seen FROM wisp_traffic_rollups'''+scope+'''
                    UNION ALL SELECT MIN(start_at),MAX(end_at) FROM wisp_traffic_intervals'''+scope+') measurement_extent',params+params)[0]
                start=start or bounds['first_seen'] or clock.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(STORAGE).replace(tzinfo=None)
                end=end or max((bounds['last_seen'] or start)+dt.timedelta(microseconds=1),clock.astimezone(STORAGE).replace(tzinfo=None))
                from services.traffic_point_resolution_service import load_aliases,resolve,normalize_selection
                aliases=load_aliases(query,owner,selected.get('nas'));selected=normalize_selection(selected,aliases)
                rows=read_points(query,start,end,selected,owner,aliases)
                options=query('''SELECT DISTINCT point_key server_key,nas_address,service_type,server_name,server_interface,point_known
                    FROM wisp_traffic_rollups'''+scope+''' UNION SELECT DISTINCT point_key,nas_address,service_type,server_name,server_interface,point_known
                    FROM wisp_traffic_intervals'''+scope,params+params)
                options=list({p['server_key']:p for p in (resolve(r,aliases) for r in options)}.values())
                addresses={r['nas_address'] for r in options if r['nas_address']}
                names={}
                if addresses:
                    slots=','.join('%s' for _ in addresses);values=tuple(sorted(addresses))
                    for r in query('SELECT ip_address address,name label FROM wisp_nas_devices WHERE ip_address IN ('+slots+')',values)+query('SELECT nasname address,shortname label FROM nas WHERE nasname IN ('+slots+')',values):
                        if r['label']:names.setdefault(r['address'],r['label'])
            partial_start=bool(state[0].get('started_at') and start<state[0]['started_at'])
            for r in rows:
                for field in ('download','upload'):
                    if r[field] is not None:r[field]=int(r[field])
                r['unknown_download']=int(r.get('unknown_download') or 0)+int(partial_start)
                r['unknown_upload']=int(r.get('unknown_upload') or 0)+int(partial_start)
                r.update(unknown_sessions=0,unknown_date=0,open_sessions=0)
                r['total']=None if r['download'] is None or r['upload'] is None else int(r['download'])+int(r['upload'])
                if r['last_observed']:r['last_observed']=r['last_observed'].replace(tzinfo=STORAGE).astimezone(ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M:%S')
            for r in rows+options:
                r['nas_label']=names.get(r['nas_address']) or r['nas_address'] or 'غير محدد'
                r['server_label']=connection_point_label(r['server_name'],r['service_type'],r['server_interface'],separator=' / ')
            key=SORT_KEYS[selected['traffic_sort']]
            rows.sort(key=lambda r:r['server_key']);rows=sorted((r for r in rows if r[key] is not None),key=lambda r:r[key],reverse=selected['traffic_dir']=='desc')+[r for r in rows if r[key] is None]
            totals=sum_rows(rows)
            if not rows:totals.update(download=None,upload=None,total=None)
            chart=sorted(rows,key=lambda r:r['total'] if r['total'] is not None else -1,reverse=True)
            if len(chart)>12:chart=chart[:12]+[dict(sum_rows(chart[12:]),nas_label='',server_label='بقية السيرفرات')]
            from services.network_traffic_service import PERIODS
            return dict(selected=selected,periods=PERIODS,points=rows,chart_points=chart,totals=totals,
                nas_options=sorted({r['nas_address']:r['nas_label'] for r in options if r['nas_address']}.items(),key=lambda r:r[1]),
                server_options=sorted(options,key=lambda r:(r['nas_label'],r['server_label'])),filter_timezone=timezone,
                generated_at=clock,missing_points=sum(int(r['sessions'] or 0) for r in rows if not r['point_known']),
                measurement_started_at=state[0].get('started_at'),**navigation(selected))
        finally:conn.rollback()
