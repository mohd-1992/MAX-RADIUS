"""Read-only measured consumption, with an explicit historical session-start view."""
import datetime as dt
import hashlib
import ipaddress
import re
from zoneinfo import ZoneInfo

from services.sales_reporting_service import date_bounds
from services.sales_source_service import server_key
from services.session_display_service import connection_point_label

PERIODS = [('today','اليوم'),('yesterday','الأمس'),('this_week','آخر 7 أيام'),
           ('this_month','هذا الشهر'),('last_month','الشهر السابق'),('this_year','هذا العام'),
           ('all','كامل المدة'),('custom','نطاق مخصص')]
SORT_KEYS = {'nas':'nas_label','server':'server_label','service':'service_type',
             'sessions':'sessions','download':'download','upload':'upload','total':'total'}


def selected_filters(args, now=None, timezone='Asia/Aden'):
    selected = {k:str(args.get(k,default) or default).strip() for k,default in
                [('period','today'),('date_from',''),('date_to',''),('nas',''),
                 ('service_type',''),('server',''),('traffic_sort','total'),('traffic_dir','desc'),('basis','measured')]}
    date_bounds(selected['period'],selected['date_from'],selected['date_to'],now,timezone)
    if selected['service_type'] not in ('','hotspot','pppoe','unknown'):
        raise ValueError('نوع الخدمة غير صالح.')
    if selected['nas'] and selected['nas']!='unknown':
        try: selected['nas']=str(ipaddress.ip_address(selected['nas']))
        except ValueError: raise ValueError('جهاز الميكروتيك غير صالح.')
    if selected['server'] and selected['server']!='unknown' and not re.fullmatch('[0-9a-f]{64}',selected['server']):
        raise ValueError('السيرفر الداخلي غير صالح.')
    if selected['basis'] not in ('measured','sessions'):raise ValueError('أساس قياس الاستهلاك غير صالح.')
    if selected['traffic_sort'] not in SORT_KEYS or selected['traffic_dir'] not in ('asc','desc'):
        raise ValueError('الترتيب المطلوب غير صالح.')
    return selected


def sessions_sql(owner=None, start=None, end=None, metadata_only=False, nas=None):
    """Deduplicate live/archive/import copies by complete stable session identity.

    Conflicting packet sources stay unknown; Hotspot bridge ports do not split a
    server, whereas PPPoE interfaces do. No package or sale is used for attribution.
    """
    parts, values = [], []
    from services.session_service_evidence import evidence_join,protocol_fields
    for table in ('radacct_owned','radacct_archive_owned','wisp_imported_session_history_owned','wisp_traffic_history_owned'):
        text_fields=('acctuniqueid','username','acctsessionid','nasipaddress',
                     'calledstationid','nasportid','framedprotocol','servicetype')
        proven=protocol_fields(table)
        columns=','.join(f"CONVERT(COALESCE({proven.get(name,table+'.'+name)},'') USING utf8mb4) COLLATE utf8mb4_bin AS {name}" for name in text_fields)
        clauses=["COALESCE(acctterminatecause,'')<>'Consolidated-Historical-Import'"]
        clauses.append(f'''(sealed.acctuniqueid IS NULL OR {table}.acctinputoctets IS NULL OR {table}.acctoutputoctets IS NULL
            OR {table}.acctinputoctets<0 OR {table}.acctoutputoctets<0
            OR {table}.acctinputoctets>sealed.input_bytes OR {table}.acctoutputoctets>sealed.output_bytes)''')
        if owner is not None:
            from database.traffic_history_schema import owner_scope_sql
            scope,count=owner_scope_sql(table)
            clauses.append(scope)
            values.extend([int(owner)]*count)
        if nas not in (None,'','all'):
            clauses.append("COALESCE("+table+".nasipaddress,'')=%s"); values.append('' if nas=='unknown' else nas)
        if start:
            clauses.append(table+'.acctstarttime>=%s'); values.append(start)
        if end:
            clauses.append(table+'.acctstarttime<%s'); values.append(end)
        fallback=(table+'.source_fallback' if table=='wisp_traffic_history_owned' else
            f"CASE WHEN COALESCE({table}.acctuniqueid,'')='' AND COALESCE({table}.acctsessionid,'')='' AND {table}.acctstarttime IS NULL THEN CONCAT('{table}:',{table}.radacctid) ELSE '' END")
        def counter(column,sealed):
            return f'''CASE WHEN {table}.{column}>=0 THEN
                CASE WHEN sealed.acctuniqueid IS NULL THEN {table}.{column}
                ELSE GREATEST(0,CAST({table}.{column} AS DECIMAL(30,0))-sealed.{sealed}) END END AS {column}'''
        counters='' if metadata_only else f',{table}.acctstoptime,{table}.acctupdatetime,'+counter('acctinputoctets','input_bytes')+','+counter('acctoutputoctets','output_bytes')
        parts.append(f'''SELECT {columns},{table}.acctstarttime{counters},
            {fallback} AS fallback_id,CASE WHEN sealed.acctuniqueid IS NULL THEN 1 ELSE 0 END source_session_weight
            FROM {table} LEFT JOIN wisp_account_usage_seals sealed ON sealed.acctuniqueid=CONVERT({table}.acctuniqueid USING ascii)
            {evidence_join(table)}
            WHERE {' AND '.join(clauses)}''')
    kind="""CASE WHEN LOWER(TRIM(framedprotocol))='ppp' AND LOWER(TRIM(servicetype)) IN ('login-user','login') THEN 'conflict'
        WHEN LOWER(TRIM(framedprotocol))='ppp' THEN 'pppoe'
        WHEN LOWER(TRIM(servicetype)) IN ('login-user','login') THEN 'hotspot' ELSE '' END"""
    normalized=f"SELECT raw.*, {kind} AS kind FROM ({' UNION ALL '.join(parts)}) raw"
    counter_aggregates='''        MAX(CASE WHEN acctoutputoctets>=0 THEN acctoutputoctets END) AS download,
        MAX(CASE WHEN acctinputoctets>=0 THEN acctinputoctets END) AS upload,
        MAX(acctstoptime) AS stopped_at,MAX(COALESCE(acctstoptime,acctupdatetime,acctstarttime)) AS observed_at''' if not metadata_only else ''
    grouped=f'''SELECT acctuniqueid,username,acctsessionid,nasipaddress,acctstarttime,
        CASE WHEN MIN(NULLIF(kind,''))=MAX(NULLIF(kind,'')) AND MAX(kind)<>'conflict'
            THEN COALESCE(MAX(NULLIF(kind,'')),'') ELSE '' END AS service_type,
        CASE WHEN MIN(NULLIF(calledstationid,''))=MAX(NULLIF(calledstationid,''))
            THEN COALESCE(MAX(NULLIF(calledstationid,'')),'') ELSE '' END AS server_name,
        CASE WHEN MIN(NULLIF(nasportid,''))=MAX(NULLIF(nasportid,''))
            THEN COALESCE(MAX(NULLIF(nasportid,'')),'') ELSE '' END AS server_interface{',' if not metadata_only else ''}
        {counter_aggregates},MAX(source_session_weight) AS session_weight,0 AS unknown_sessions
        FROM ({normalized}) normalized
        GROUP BY acctuniqueid,username,acctsessionid,nasipaddress,acctstarttime,fallback_id'''
    # Settled summaries are explicitly weighted aggregates, never individual sessions.
    clauses=['h.report_visible=1']; summary_values=[];complete=[];complete_values=[]
    if owner is not None:
        clauses.append('h.reseller_id=%s');summary_values.append(int(owner))
    if nas not in (None,'','all'):
        clauses.append("h.nasipaddress=%s");summary_values.append('' if nas=='unknown' else nas)
    if start:
        clauses.append('h.last_start>=%s');summary_values.append(start)
        complete.append('h.first_start>=%s');complete_values.append(start)
    if end:
        clauses.append('h.first_start<%s');summary_values.append(end)
        complete.append('h.last_start<%s');complete_values.append(end)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    text=lambda expr: 'CONVERT('+expr+' USING utf8mb4) COLLATE utf8mb4_bin'
    kind_summary="CASE WHEN LOWER(TRIM(h.framedprotocol))='ppp' AND LOWER(TRIM(h.servicetype)) IN ('login-user','login') THEN '' WHEN LOWER(TRIM(h.framedprotocol))='ppp' THEN 'pppoe' WHEN LOWER(TRIM(h.servicetype)) IN ('login-user','login') THEN 'hotspot' ELSE '' END"
    full=' AND '.join(complete) or '1=1'
    summary_base=f"SELECT {text('LEFT(h.summary_key,32)')} AS acctuniqueid,{text('COALESCE(v.username,h.username)')} AS username,{text('h.summary_key')} AS acctsessionid,{text('h.nasipaddress')} AS nasipaddress,h.first_start AS acctstarttime,{text(kind_summary)} AS service_type,{text('h.calledstationid')} AS server_name,{text('h.nasportid')} AS server_interface,h.output_bytes,h.input_bytes,h.first_start,h.last_start,h.session_count,({full}) date_complete FROM wisp_account_usage_summary h LEFT JOIN wisp_vouchers v ON v.id=h.voucher_id{where}"
    extra='' if metadata_only else ',CASE WHEN date_complete THEN output_bytes END AS download,CASE WHEN date_complete THEN input_bytes END AS upload,first_start AS stopped_at,last_start AS observed_at'
    summary=f'''SELECT acctuniqueid,username,acctsessionid,nasipaddress,acctstarttime,service_type,server_name,server_interface{extra},
        CASE WHEN date_complete THEN session_count ELSE 0 END session_weight,
        CASE WHEN date_complete THEN 0 ELSE session_count END unknown_sessions FROM ({summary_base}) summary_rows'''
    return '('+grouped+' UNION ALL '+summary+') sessions', tuple(values+complete_values+summary_values)


def point_info(row, catalog):
    nas=str(row.get('nas_address') or '').strip()
    try:
        address=ipaddress.ip_address(nas)
        nas='' if address.is_loopback or address.is_unspecified else str(address)
    except ValueError: nas=''
    kind=row.get('service_type') or ''
    name=row.get('server_name') or ''
    interface=(row.get('server_interface') or '') if kind=='pppoe' else ''
    if any(ord(c)<32 or ord(c)==127 for c in name+interface): name=interface=''
    key=server_key(nas,kind,name,interface)
    known=key!='unknown'
    if not known:
        # A recorded name remains useful when protocol/interface metadata is
        # incomplete. Keep distinct names apart without asserting a service.
        fields=(nas,kind,name)
        identity=('recorded-point:'+''.join(str(len(v))+':'+v for v in fields)
                  if name else 'unknown:'+nas+':'+kind)
        key=hashlib.sha256(identity.encode()).hexdigest()
    label=connection_point_label(name,kind,interface,separator=' / ')
    return dict(server_key=key,nas_address=nas,nas_label=catalog.get(nas) or nas or 'غير محدد',server_name=name,server_interface=interface,
                service_type=kind,server_label=label,point_known=known)


def sum_rows(rows):
    result={field:sum(int(r.get(field) or 0) for r in rows)
            for field in ('sessions','unknown_sessions','unknown_download','unknown_upload','unknown_date','open_sessions')}
    for field in ('download','upload'):
        known=[r[field] for r in rows if r.get(field) is not None]
        result[field]=sum(int(value) for value in known) if known else (None if result['sessions'] or result['unknown_sessions'] or result['unknown_'+field] else 0)
    result['total']=None if result['download'] is None or result['upload'] is None else result['download']+result['upload']
    return result


def matches_point(point, selected):
    return ((not selected['nas'] or point['nas_address']==('' if selected['nas']=='unknown' else selected['nas']))
        and (not selected['service_type'] or point['service_type']==('' if selected['service_type']=='unknown' else selected['service_type']))
        and (not selected['server'] or (not point['point_known'] if selected['server']=='unknown' else point['server_key']==selected['server'])))


def report(args, now=None, timezone=None):
    from core.ownership import owner_id
    from core.time_service import get_configured_timezone_name
    from database.db import get_connection
    timezone=timezone or get_configured_timezone_name()
    selected=selected_filters(args,now,timezone)
    start,end=date_bounds(selected['period'],selected['date_from'],selected['date_to'],now,timezone)
    owner=owner_id()
    if selected['basis']=='measured':
        from services.measured_traffic_service import report as measured_report
        return measured_report(selected,now,timezone,owner)
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            def query(sql,params=()):
                cur.execute(sql,params)
                return list(cur.fetchall())
            from services.report_catalog_cache import catalog_query
            options_query=catalog_query(query,owner)
            catalog={}
            # Restricted users only see NAS names for their owned session rows.
            scope,scope_params=sessions_sql(owner,metadata_only=True)
            allowed='' if owner is None else f' WHERE address IN (SELECT DISTINCT nasipaddress FROM {scope})'
            for item in options_query('''SELECT address,label FROM (
                SELECT ip_address AS address,name AS label,0 AS priority,id FROM wisp_nas_devices
                UNION ALL SELECT nasname,shortname,1,id FROM nas) catalog'''+allowed+' ORDER BY priority,id',scope_params if owner is not None else ()):
                if item['address'] and str(item['label'] or '').strip():
                    catalog.setdefault(item['address'],item['label'].strip())
            # The filter catalog is all-time and independent of the period.
            def grouped(source,params):
                return query(f'''SELECT nasipaddress AS nas_address,service_type,server_name,
                    CASE WHEN service_type='pppoe' THEN server_interface ELSE '' END AS server_interface,
                    SUM(session_weight) AS sessions,SUM(download) AS download,SUM(upload) AS upload,
                    SUM(download IS NULL) AS unknown_download,SUM(upload IS NULL) AS unknown_upload,
                    SUM(acctstarttime IS NULL) AS unknown_date,SUM(stopped_at IS NULL) AS open_sessions,
                    SUM(unknown_sessions) AS unknown_sessions,
                    MAX(observed_at) AS last_observed
                    FROM {source} GROUP BY nasipaddress,service_type,server_name,
                    CASE WHEN service_type='pppoe' THEN server_interface ELSE '' END''',params)
            source,params=sessions_sql(owner,start,end,nas=selected.get('nas'))
            rows=grouped(source,params)
            if not start and not end and not selected.get('nas'):
                # The all-time aggregate already contains every catalog dimension.
                # Reuse this request's exact deduplication, including conflicts and
                # settled summaries, rather than grouping history a second time.
                option_rows=rows
            else:
                option_rows=options_query(f'''SELECT DISTINCT nasipaddress AS nas_address,service_type,server_name,
                    CASE WHEN service_type='pppoe' THEN server_interface ELSE '' END AS server_interface
                    FROM {scope}''',scope_params)
        conn.rollback()
    finally:
        conn.close()
    options={}
    for row in option_rows:
        point=point_info(row,catalog); options[point['server_key']]=point
    combined={}
    for row in rows:
        point=point_info(row,catalog)
        key=point['server_key']
        combined.setdefault(key,dict(point,parts=[]))['parts'].append(row)
    points=[]
    for point in combined.values():
        parts=point.pop('parts')
        point.update(sum_rows(parts))
        point['last_observed']=max((r['last_observed'] for r in parts if r.get('last_observed')),default=None)
        if point['last_observed']:
            observed=point['last_observed']
            if isinstance(observed,str): observed=dt.datetime.fromisoformat(observed)
            if observed.tzinfo is None: observed=observed.replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
            point['last_observed']=observed.astimezone(ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M:%S')
        if not matches_point(point,selected):continue
        points.append(point)
    # Unknown counters are always last, even for descending order.
    key=SORT_KEYS[selected['traffic_sort']]
    points.sort(key=lambda p:p['server_key'])
    points=sorted((p for p in points if p[key] is not None),key=lambda p:p[key],reverse=selected['traffic_dir']=='desc')+[p for p in points if p[key] is None]
    totals=sum_rows(points)
    chart_points=sorted(points,key=lambda p:p['total'] if p['total'] is not None else -1,reverse=True)
    if len(chart_points)>12:
        rest=sum_rows(chart_points[12:])
        chart_points=chart_points[:12]+[dict(rest,nas_label='',server_label='بقية السيرفرات')]
    nas_options=sorted({p['nas_address']:p['nas_label'] for p in options.values() if p['nas_address']}.items(),key=lambda p:p[1])
    from services.report_navigation_service import navigation
    return dict(selected=selected,periods=PERIODS,points=points,chart_points=chart_points,totals=totals,nas_options=nas_options,
                server_options=sorted(options.values(),key=lambda p:(p['nas_label'],p['server_label'])),
                filter_timezone=timezone,generated_at=(now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(timezone)),
                missing_points=sum(p['sessions'] for p in points if not p['point_known']),**navigation(selected))


def format_bytes(value):
    if value is None:return 'غير متاح'
    value=int(value)
    for unit,power in [('TiB',4),('GiB',3),('MiB',2),('KiB',1)]:
        if value>=1024**power:return f'{value/(1024**power):,.2f} {unit}'
    return f'{value:,} B'
