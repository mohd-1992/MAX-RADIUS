"""One read-only, historically priced report contract for page and exports."""
import datetime as dt
import math
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

ZERO = Decimal('0.00')
ARABIC_MONTHS = ('يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر')


def month_label(value):
    if not value:
        return 'تاريخ غير محدد'
    year, month = value.split('-')
    return ARABIC_MONTHS[int(month)-1]+' '+year


def accounting_rows_sql(owner=None):
    # Lifetime session counters, not interval deltas. A copied archive session
    # contributes once, taking the greatest recorded counters across copies.
    parts = []
    text_columns = ('acctuniqueid','username','acctsessionid','nasipaddress',
                    'calledstationid','nasportid','framedprotocol','servicetype')
    columns = ','.join(f'CONVERT({name} USING utf8mb4) COLLATE utf8mb4_bin AS {name}' for name in text_columns)
    for table in ('radacct_owned','radacct_archive_owned','wisp_imported_session_history_owned','wisp_traffic_history_owned'):
        from database.traffic_history_schema import owner_scope_sql
        condition,count=owner_scope_sql(table)
        # Owner is a validated integer, never request-supplied SQL text.
        scope = '' if owner is None else ' AND '+condition.replace('%s',str(int(owner)))
        parts.append(f'''SELECT {columns},acctstarttime,acctinputoctets,acctoutputoctets
            FROM {table} WHERE acctstarttime IS NOT NULL
            AND COALESCE(acctterminatecause,'')<>'Consolidated-Historical-Import'{scope}''')
    return '('+' UNION ALL '.join(parts)+') a'


def legacy_traffic_for_period(query, selected, owner=None, now=None, timezone='Asia/Aden'):
    start, end = date_bounds(selected['period'],selected['date_from'],selected['date_to'],now,timezone)
    monthly = selected['period'] in ('all','this_year') or bool(start and end and (end-start).days>62)
    from services.network_traffic_service import sessions_sql
    from services.dashboard_reporting_service import windows_sql
    source, params = sessions_sql(owner, start, end)
    storage = dt.timezone(dt.timedelta(hours=3))
    def local_date(value):
        if isinstance(value,str): value=dt.datetime.fromisoformat(value)
        return value.replace(tzinfo=storage).astimezone(ZoneInfo(timezone)).date()
    if not start or not end:
        extent=query(f'SELECT MIN(acctstarttime) AS first_seen,MAX(acctstarttime) AS last_seen FROM {source}',params)[0]
        if not extent['first_seen']:
            return dict(labels=[],download=[],upload=[],unknown_counters=0)
    first=local_date(start) if start else local_date(extent['first_seen'])
    last=local_date(end-dt.timedelta(microseconds=1)) if end else local_date(extent['last_seen'])
    windows=[]
    date=first.replace(day=1) if monthly else first
    while date<=last:
        if monthly:
            following=(date.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
            windows.append((date.strftime('%Y-%m'),date,following-dt.timedelta(days=1)))
        else:
            following=date+dt.timedelta(days=1)
            windows.append((date.isoformat(),date,date))
        date=following
    bucket,values=windows_sql(windows,'acctstarttime',timezone)
    rows=query(f"""SELECT {bucket} AS bucket,SUM(download) AS download,SUM(upload) AS upload,
        SUM(download IS NULL OR upload IS NULL) AS unknown_counters
        FROM {source} GROUP BY bucket ORDER BY bucket""",values+params)
    return dict(labels=[month_label(r['bucket']) if monthly else r['bucket'] for r in rows],
                download=[None if r['download'] is None else float(r['download'])/1073741824 for r in rows],
                upload=[None if r['upload'] is None else float(r['upload'])/1073741824 for r in rows],
                unknown_counters=sum(int(r['unknown_counters'] or 0) for r in rows))


def traffic_for_period(query, selected, owner=None, now=None, timezone='Asia/Aden'):
    from services.traffic_sample_service import series
    return series(query,selected,owner,now,timezone)


def date_bounds(period, start='', end='', now=None, timezone='Asia/Aden'):
    zone = ZoneInfo(timezone)
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(zone).date()
    first = last = None
    if period == 'today':
        first = last = today
    elif period == 'yesterday':
        first = last = today - dt.timedelta(days=1)
    elif period == 'this_week':
        first, last = today - dt.timedelta(days=6), today
    elif period == 'this_month':
        first, last = today.replace(day=1), today
    elif period == 'last_month':
        last = today.replace(day=1) - dt.timedelta(days=1)
        first = last.replace(day=1)
    elif period == 'this_year':
        first, last = today.replace(month=1, day=1), today
    elif period == 'custom':
        try:
            first = dt.date.fromisoformat(start) if start else None
            last = dt.date.fromisoformat(end) if end else None
        except ValueError:
            raise ValueError('أدخل تاريخًا صحيحًا للفترة المطلوبة.')
        if first and last and first > last:
            raise ValueError('تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.')
    elif period != 'all':
        raise ValueError('الفترة المطلوبة غير صالحة.')
    storage = dt.timezone(dt.timedelta(hours=3))
    def convert(date):
        return dt.datetime.combine(date, dt.time.min, zone).astimezone(storage).replace(tzinfo=None)
    return convert(first) if first else None, convert(last + dt.timedelta(days=1)) if last else None


def filters(args, owner=None, now=None, timezone='Asia/Aden'):
    out = {key: str(args.get(key, default) or default).strip() for key, default in (
        ('period','today'),('date_from',''),('date_to',''),('reseller_id',''),('package_name',''),
        ('q',''),('service_type',''),('nas',''),('server',''),('record_kind','voucher'),('source_quality',''))}
    if out['record_kind'] not in ('voucher','invoice'):
        raise ValueError('نوع السجل غير صالح.')
    if out['source_quality'] not in ('','confirmed','inferred','incomplete','ambiguous'):
        raise ValueError('حالة تحديد المصدر غير صالحة.')
    if out['service_type'] not in ('','all','hotspot','pppoe','unknown'):
        raise ValueError('نوع الخدمة غير صالح.')
    if out['server'] not in ('','all','unknown') and not re.fullmatch('[a-f0-9]{64}',out['server']):
        raise ValueError('معرف السيرفر غير صالح.')
    if out['reseller_id'] not in ('','all','direct'):
        if not out['reseller_id'].isdigit() or int(out['reseller_id']) <= 0:
            raise ValueError('الموزع المحدد غير صالح.')
    if any(len(out[k]) > n for k,n in (('q',200),('package_name',128),('nas',128))):
        raise ValueError('قيمة الفلتر أطول من المسموح.')
    clauses, values = ['e.event_kind=%s'], [out['record_kind']]
    if owner is not None:
        clauses.extend(["e.event_kind='voucher'",'e.reseller_id=%s'])
        values.append(int(owner))
    start, end = date_bounds(out['period'],out['date_from'],out['date_to'],now,timezone)
    if start:
        clauses.append('e.event_at>=%s'); values.append(start)
    if end:
        clauses.append('e.event_at<%s'); values.append(end)
    if out['reseller_id'] == 'direct':
        clauses.append('e.reseller_id IS NULL')
    elif out['reseller_id'] not in ('','all'):
        clauses.append('e.reseller_id=%s'); values.append(int(out['reseller_id']))
    if out['package_name'] not in ('','all'):
        clauses.append('e.package_name=%s'); values.append(out['package_name'])
    if out['q']:
        clauses.append('(e.username LIKE %s OR e.serial_number LIKE %s OR e.batch_name LIKE %s)')
        values.extend(['%'+out['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%']*3)
    if out['service_type'] not in ('','all'):
        clauses.append("COALESCE(x.service_type,'')=%s")
        values.append('' if out['service_type']=='unknown' else out['service_type'])
    if out['nas'] not in ('','all'):
        clauses.append("COALESCE(x.nas_address,'')=%s"); values.append('' if out['nas']=='unknown' else out['nas'])
    if out['server'] not in ('','all'):
        clauses.append("COALESCE(x.server_key,'unknown')=%s"); values.append(out['server'])
    if out['source_quality']=='incomplete':
        clauses.append("COALESCE(x.resolution_status,'unknown') IN ('unknown','partial')")
    elif out['source_quality']:
        clauses.append('x.resolution_status=%s');values.append(out['source_quality'])
    return out, ' AND '.join(clauses), tuple(values)


# Record types are intentionally separate: a wallet funding sale and the later
# invoice spending that wallet must never be added as independent network income.
LEDGER = '''(SELECT 'voucher' AS event_kind,s.id,s.activated_at AS event_at,s.username,s.serial_number,s.batch_name,
    s.package_name,s.price,s.cost,s.reseller_id,
    CASE WHEN s.first_activation_voucher_id IS NOT NULL THEN 'تفعيل كرت' ELSE 'بيع / تجديد كرت' END AS operation
    FROM wisp_voucher_sales s
    UNION ALL
    SELECT 'invoice',i.id,i.paid_at,COALESCE(i.subscriber_name,i.invoice_number),i.invoice_number,'فواتير المشتركين',
    COALESCE(i.package_name,''),i.report_cost,i.report_cost,NULL,'فاتورة مشترك مدفوعة'
    FROM wisp_invoices i WHERE i.status='paid'
      AND COALESCE(i.package_name,'') NOT IN ('إيداع رصيد محفظة','سحب/تصحيح رصيد محفظة')) e'''
FROM = LEDGER+''' LEFT JOIN wisp_sale_sources x ON x.event_kind=e.event_kind AND x.event_id=e.id'''


def report_source(record_kind):
    """Choose the validated ledger branch before joining/filtering the report.

    Keep LEDGER unchanged for the dashboard's combined income series. The page
    already requires exactly one record kind; no monetary record is cached.
    """
    if record_kind not in ('voucher','invoice'):
        raise ValueError('نوع السجل غير صالح.')
    voucher, invoice = LEDGER[1:-3].split('    UNION ALL\n', 1)
    # UNION takes column names from its first branch. When selecting invoices
    # alone, retain those names explicitly. Cost never rewrites the invoice amount.
    invoice = '''SELECT 'invoice' AS event_kind,i.id,i.paid_at AS event_at,
        COALESCE(i.subscriber_name,i.invoice_number) AS username,
        i.invoice_number AS serial_number,'فواتير المشتركين' AS batch_name,
        COALESCE(i.package_name,'') AS package_name,i.report_cost AS price,i.report_cost AS cost,
        NULL AS reseller_id,'فاتورة مشترك مدفوعة' AS operation
        FROM wisp_invoices i WHERE i.status='paid'
        AND COALESCE(i.package_name,'') NOT IN ('إيداع رصيد محفظة','سحب/تصحيح رصيد محفظة')'''
    branch = voucher if record_kind=='voucher' else invoice
    return '('+branch+') e LEFT JOIN wisp_sale_sources x ON x.event_kind=e.event_kind AND x.event_id=e.id'
# Resolve display names only; historical NAS identity and source snapshots stay intact.
NAS_LABEL = """COALESCE(
    (SELECT NULLIF(TRIM(n.name),'') FROM wisp_nas_devices n
     WHERE n.ip_address=x.nas_address AND NULLIF(TRIM(n.name),'') IS NOT NULL ORDER BY n.id LIMIT 1),
    (SELECT NULLIF(TRIM(n.shortname),'') FROM nas n
     WHERE n.nasname=x.nas_address AND NULLIF(TRIM(n.shortname),'') IS NOT NULL ORDER BY n.id LIMIT 1),
    NULLIF(x.nas_label,''),x.nas_address,'')"""
ISP = 'CASE WHEN e.reseller_id IS NOT NULL THEN e.cost ELSE e.price END'
COMMISSION = 'CASE WHEN e.reseller_id IS NOT NULL THEN e.price-e.cost ELSE 0 END'
MONEY_SQL = f'''COUNT(*) AS sold_count,COALESCE(SUM(e.price),0) AS total_gross_revenue,
    COALESCE(SUM({ISP}),0) AS total_isp_revenue,COALESCE(SUM({COMMISSION}),0) AS total_reseller_profit,
    SUM(CASE WHEN e.reseller_id IS NOT NULL AND e.cost IS NULL THEN 1 ELSE 0 END) AS unknown_cost_count,
    COALESCE(SUM(e.price IS NULL),0) AS unknown_amount_count,
    COALESCE(SUM(x.resolution_status='inferred'),0) AS inferred_count,
    COALESCE(SUM(x.resolution_status='ambiguous'),0) AS ambiguous_count,
    COALESCE(SUM(COALESCE(x.service_type,'')=''),0) AS missing_service_count,
    COALESCE(SUM(COALESCE(x.server_key,'unknown')='unknown'),0) AS missing_server_count,
    COALESCE(SUM(COALESCE(x.resolution_version,0)<2),0) AS unreviewed_count'''


def export_value(value):
    """User-controlled text is literal data, never an Excel/CSV formula."""
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
        return "'"+value
    return value


def _label(row):
    from services.session_display_service import connection_point_label
    return connection_point_label(row.get('server_name'),row.get('service_type'),
                                  row.get('server_interface'),separator=' / ')


SORT_COLUMNS = {
    'package': {'name': ('name',), 'service': ('service_type',),
        'price': ('minimum_price','maximum_price'), 'share': ('minimum_isp','maximum_isp'),
        'count': ('sold_count',), 'gross': ('total_gross_revenue',),
        'net': ('total_isp_revenue',), 'commission': ('total_reseller_profit',)},
    'server': {'nas': ("COALESCE(NULLIF(nas_label,''),nas_address)",),
        'server': ('service_type','server_name','server_interface'), 'count': ('sold_count',),
        'gross': ('total_gross_revenue',), 'net': ('total_isp_revenue',),
        'commission': ('total_reseller_profit',)},
    'log': {'id': ('e.id',), 'username': ('e.username',), 'batch': ('e.batch_name',),
        'package': ('e.package_name',), 'price': ('e.price',), 'share': (ISP,),
        'commission': (COMMISSION,), 'reseller': ('reseller_name',),
        'server': ("COALESCE(x.service_type,'')","COALESCE(x.server_name,'')","COALESCE(x.server_interface,'')"),
        'date': ('e.event_at',)}
}
SORT_DEFAULTS = {'package': ('name','asc'), 'server': ('gross','desc'), 'log': ('date','desc')}


def sort_settings(args):
    result = {}
    for table, (key, direction) in SORT_DEFAULTS.items():
        key = str(args.get(table+'_sort', key) or key)
        direction = str(args.get(table+'_dir', direction) or direction)
        if key not in SORT_COLUMNS[table] or direction not in ('asc','desc'):
            raise ValueError('ترتيب الجدول غير صالح.')
        result[table] = {'key':key, 'direction':direction}
    return result


def sort_order(table, settings):
    selected = settings[table]
    # Only fixed expressions and directions enter SQL; request text never does.
    expressions = SORT_COLUMNS[table][selected['key']]
    terms = []
    for expression in expressions:
        terms.extend([f'({expression} IS NULL) ASC', expression+' '+selected['direction'].upper()])
    terms.append({'package':'name ASC,service_type ASC', 'server':'server_key ASC',
                  'log':'e.event_at DESC,e.event_kind ASC,e.id DESC'}[table])
    return ','.join(terms)


def package_metrics(packages):
    """Package groups partition the filtered ledger; sum with exact Decimal types."""
    fields=('sold_count','total_gross_revenue','total_isp_revenue','total_reseller_profit',
            'unknown_cost_count','unknown_amount_count','inferred_count','ambiguous_count','missing_service_count',
            'missing_server_count','unreviewed_count')
    if not packages:
        return {key:ZERO if key in ('total_gross_revenue','total_isp_revenue','total_reseller_profit') else None if key=='unknown_cost_count' else 0 for key in fields}
    return {key:sum((row[key] if row.get(key) is not None else 0) for row in packages) for key in fields}


def report(args, export=False, now=None, timezone=None):
    from database.db import get_connection
    from core.ownership import owner_id
    if timezone is None:
        from core.time_service import get_configured_timezone_name
        timezone = get_configured_timezone_name()
    selected, where, params = filters(args,owner_id(),now,timezone)
    report_from = report_source(selected['record_kind'])
    table_sorts = sort_settings(args)
    try:
        page = max(1,int(args.get('page',1)))
    except (ValueError,TypeError):
        page = 1
    per_page = 50
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            def query(sql, values=()):
                cur.execute(sql,values)
                return list(cur.fetchall())
            from services.report_catalog_cache import catalog_query
            options_query=catalog_query(query,owner_id())
            packages=[]
            if not export:
                packages = query(f'''SELECT * FROM (SELECT e.package_name AS name,
                    COALESCE(GROUP_CONCAT(DISTINCT COALESCE(NULLIF(x.service_type,''),CASE WHEN x.package_service_type IN ('hotspot','pppoe','both') THEN CONCAT('package:',x.package_service_type) END) ORDER BY x.service_type SEPARATOR ','),'') AS service_type,
                    MIN(e.price) AS minimum_price,MAX(e.price) AS maximum_price,
                    MIN({ISP}) AS minimum_isp,MAX({ISP}) AS maximum_isp,{MONEY_SQL}
                    FROM {report_from} WHERE {where} GROUP BY e.package_name) package_groups
                    ORDER BY {sort_order('package',table_sorts)}''',params)
            metrics = query('SELECT '+MONEY_SQL+' FROM '+report_from+' WHERE '+where,params)[0] if export else package_metrics(packages)
            total = int(metrics['sold_count'])
            if export and total>50000:
                raise ValueError('النتائج تتجاوز 50000 عملية؛ حدد فترة أصغر لتصدير كامل دون اقتطاع.')
            pages = max(1,math.ceil(total/per_page)); page = min(page,pages)
            limit = total if export else per_page
            offset = 0 if export else (page-1)*per_page
            records = query(f'''SELECT e.*,COALESCE(x.server_key,'unknown') AS server_key,
                COALESCE(x.nas_address,'') AS nas_address,{NAS_LABEL} AS nas_label,
                COALESCE(x.service_type,'') AS service_type,COALESCE(x.server_name,'') AS server_name,
                COALESCE(x.package_service_type,'') AS package_service_type,
                COALESCE(x.server_interface,'') AS server_interface,COALESCE(x.attribution,'unknown') AS attribution,
                COALESCE(x.resolution_status,'unknown') AS resolution_status,
                COALESCE(x.service_attribution,'') AS service_attribution,
                COALESCE(x.resolution_reason,'') AS resolution_reason,
                COALESCE(NULLIF(m.full_name,''),m.username,CONCAT('موزع #',e.reseller_id),'مباشر (الإدارة)') AS reseller_name,
                {ISP} AS isp_share,{COMMISSION} AS reseller_margin,e.event_at AS activated_at
                FROM {report_from} LEFT JOIN wisp_managers m ON m.id=e.reseller_id WHERE {where}
                ORDER BY {sort_order('log',table_sorts)} LIMIT %s OFFSET %s''',params+(limit,offset))
            servers=[];chart=None;pkg_options=[];point_options=[]
            nas_catalog={};traffic_chart=None;resellers=[];undated=0
            if not export:
                servers = query(f'''SELECT * FROM (SELECT COALESCE(x.server_key,'unknown') AS server_key,
                    MIN(COALESCE(x.nas_address,'')) AS nas_address,MIN({NAS_LABEL}) AS nas_label,
                    MIN(COALESCE(x.service_type,'')) AS service_type,
                    CASE WHEN COUNT(NULLIF(x.server_name,''))=COUNT(*) AND MIN(NULLIF(x.server_name,''))=MAX(NULLIF(x.server_name,''))
                        THEN COALESCE(MAX(NULLIF(x.server_name,'')),'') ELSE '' END AS server_name,
                    MIN(COALESCE(x.server_interface,'')) AS server_interface,{MONEY_SQL}
                    FROM {report_from} WHERE {where}
                    GROUP BY COALESCE(x.server_key,'unknown'),COALESCE(x.nas_address,''),COALESCE(x.service_type,'')) server_groups
                    ORDER BY {sort_order('server',table_sorts)}''',params)
                from services.report_time_axis import sales_series
                chart = sales_series(query,report_from,where,params,selected,now,timezone)
                # Options share the permission scope and record kind, but remain
                # independent of the selected date/server so filters can be changed.
                _, scope, scope_params = filters({'record_kind':selected['record_kind'],'period':'all'},owner_id(),now,timezone)
                pkg_options = options_query(f'SELECT DISTINCT e.package_name AS name FROM {report_from} WHERE {scope} ORDER BY name',scope_params)
                point_options = options_query(f'''SELECT COALESCE(x.server_key,'unknown') AS server_key,
                    MIN(COALESCE(x.nas_address,'')) AS nas_address,MIN({NAS_LABEL}) AS nas_label,
                    MIN(COALESCE(x.service_type,'')) AS service_type,
                    CASE WHEN COUNT(NULLIF(x.server_name,''))=COUNT(*) AND MIN(NULLIF(x.server_name,''))=MAX(NULLIF(x.server_name,''))
                        THEN COALESCE(MAX(NULLIF(x.server_name,'')),'') ELSE '' END AS server_name,
                    MIN(COALESCE(x.server_interface,'')) AS server_interface FROM {report_from} WHERE {scope}
                    GROUP BY COALESCE(x.server_key,'unknown'),COALESCE(x.nas_address,''),COALESCE(x.service_type,'')''',scope_params)
                nas_catalog = {}
                if owner_id() is None:
                    # Catalog visibility is independent of financial activity. Do
                    # not expose global infrastructure to restricted resellers.
                    for item in options_query('SELECT ip_address AS address,name AS label FROM wisp_nas_devices')+options_query('SELECT nasname AS address,shortname AS label FROM nas'):
                        if item['address']:
                            nas_catalog.setdefault(item['address'],item['label'])
                from services.sales_source_service import source_from_rows
                def option_key(row):
                    return (row['server_key'],row.get('nas_address',''),row.get('service_type',''))
                known = {option_key(r):r for r in point_options}
                if owner_id() is None:
                    # A server evidenced by an invoice is still a known server on
                    # the voucher tab (and vice versa); this never reattributes sales.
                    for point in options_query('''SELECT server_key,MIN(nas_address) AS nas_address,
                        MIN(nas_label) AS nas_label,MIN(service_type) AS service_type,
                        MIN(server_name) AS server_name,MIN(server_interface) AS server_interface
                        FROM wisp_sale_sources WHERE server_key<>'unknown' GROUP BY server_key'''):
                        known.setdefault(option_key(point),point)
                packets = options_query('''SELECT DISTINCT nasipaddress,calledstationid,nasportid,framedprotocol,servicetype
                    FROM '''+accounting_rows_sql(owner_id())+" WHERE COALESCE(calledstationid,'')<>''")
                for packet in packets:
                    from services.sales_source_service import packet_service
                    kind = packet_service(packet)
                    point = source_from_rows([packet],kind,'catalog_accounting')
                    if point['server_key']=='unknown' and kind!='conflict':
                        from services.network_traffic_service import point_info
                        display=point_info(dict(nas_address=packet['nasipaddress'],service_type=kind,
                            server_name=packet['calledstationid'],server_interface=packet['nasportid']),nas_catalog)
                        if display['nas_address'] and display['server_label']!='غير محدد':
                            point.update(server_key=display['server_key'],nas_address=display['nas_address'],
                                service_type=kind,server_name=packet['calledstationid'],
                                server_interface=packet['nasportid'] if kind=='pppoe' else '')
                    if point['server_key']!='unknown':
                        point['nas_label'] = nas_catalog.get(point['nas_address']) or point['nas_address']
                        known.setdefault(option_key(point),point)
                point_options = list(known.values())
                traffic_chart = traffic_for_period(query,selected,owner_id(),now,timezone) if not export else None
                resellers = options_query(f'''SELECT DISTINCT m.id,m.username,m.full_name,COALESCE(NULLIF(m.full_name,''),m.username) AS name
                    FROM {report_from} JOIN wisp_managers m ON m.id=e.reseller_id WHERE {scope} ORDER BY m.id''',scope_params)
                _, undated_where, undated_params = filters(dict(selected,period='all'),owner_id(),now,timezone)
                undated = query(f'SELECT COUNT(*) AS n FROM {report_from} WHERE {undated_where} AND e.event_at IS NULL',undated_params)[0]['n']
        conn.rollback()
    finally:
        conn.close()
    for row in records+servers+point_options:
        row['server_label'] = _label(row)
    status_labels={'confirmed':'مؤكد','inferred':'مستدل من الباقة','ambiguous':'يحتاج مراجعة','partial':'غير مكتمل','unknown':'غير محدد'}
    reason_labels={'package_reference':'نوع الخدمة مستدل من مرجع الباقة؛ ليس إثباتًا تاريخيًا من الراوتر.',
        'no_matching_session':'لا توجد جلسة موثوقة تغطي وقت العملية.',
        'multiple_session_points':'توجد جلسات متزامنة على عدة سيرفرات؛ لم يُختر أحدها بالتخمين.',
        'incomplete_session_fields':'حقول الجلسة لا تكفي لتحديد السيرفر كاملًا.',
        'conflicting_service_evidence':'أدلة نوع الخدمة متعارضة؛ يلزم مراجعتها.',
        'account_identity_conflict':'هوية الحساب المرتبط لا تطابق سجل العملية.'}
    reason_labels['evidence_no_longer_available']='المصدر المحفوظ سابقًا باقٍ؛ جلسة الإثبات لم تعد متاحة في سجل الجلسات.'
    for row in records:
        row['source_status_label']=status_labels.get(row['resolution_status'],'غير محدد')
        row['source_reason_label']=reason_labels.get(row['resolution_reason'],'بيانات محفوظة مرتبطة بالعملية.')
    for row in packages:
        row['service_types'] = [kind for kind in row['service_type'].split(',') if kind]
        if row['missing_service_count']:
            row['service_types'].append('')
        row['price'] = _range(row['minimum_price'],row['maximum_price'])
        row['isp_unit_price'] = _range(row['minimum_isp'],row['maximum_isp'])
        row['reseller_unit_commission'] = 'حسب العملية'
    gross = Decimal(str(metrics['total_gross_revenue']))
    share = Decimal(str(metrics['total_isp_revenue']))
    commission = Decimal(str(metrics['total_reseller_profit']))
    result = dict(period=selected['period'],custom_date_from=selected['date_from'],custom_date_to=selected['date_to'],
        selected_reseller=selected['reseller_id'],selected_package=selected['package_name'],search_q=selected['q'],
        selected_service=selected['service_type'],selected_nas=selected['nas'],selected_server=selected['server'],
        selected_quality=selected['source_quality'],table_sorts=table_sorts,sort_defaults=SORT_DEFAULTS,
        record_kind=selected['record_kind'],page=page,per_page=per_page,total_pages=pages,total_items_count=total,
        gross_sales=gross,net_isp_revenue=share,total_reseller_commissions=commission,
        isp_share_pct=round(share/gross*100,1) if gross else ZERO,
        reseller_share_pct=round(commission/gross*100,1) if gross else ZERO,
        package_breakdown=packages,server_breakdown=servers,recent_sales=records,
        resellers_list=resellers,all_packages_list=pkg_options,server_options=point_options,
        nas_options=sorted(dict(nas_catalog,**{r['nas_address']:r['nas_label'] for r in point_options if r['nas_address'] and r['nas_address'] not in nas_catalog}).items()),
        traffic_chart=traffic_chart,
        sales_chart=chart,
        unknown_date_count=int(undated),unknown_cost_count=int(metrics['unknown_cost_count'] or 0),
        unknown_amount_count=int(metrics['unknown_amount_count'] or 0),filter_timezone=timezone)
    for key in ('inferred_count','ambiguous_count','missing_service_count','missing_server_count','unreviewed_count'):
        result[key]=int(metrics[key] or 0)
    # Every displayed sum must reconcile to the same filtered ledger, including
    # the unknown-server bucket; joins must never multiply financial rows.
    for field in (() if export else ('sold_count','total_gross_revenue','total_isp_revenue','total_reseller_profit')):
        assert sum((r[field] for r in packages),0)==metrics[field]
        assert sum((r[field] for r in servers),0)==metrics[field]
    from services.report_navigation_service import navigation
    result.update(navigation(args))
    return result


def _range(low, high):
    if low is None or high is None:
        return 'غير محدد'
    return f'{low:,.2f}' if low==high else f'{low:,.2f} – {high:,.2f}'
