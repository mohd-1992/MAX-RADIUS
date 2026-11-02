"""Read-only, deduplicated account session history across all retained stores."""
from core.rate_limit import format_bytes, format_duration
from services.session_display_service import enrich_session_display


def history_sql(username, owner):
    from database.traffic_history_schema import owner_scope_sql
    parts, params = [], []
    stores = ('radacct_owned', 'radacct_archive_owned',
              'wisp_imported_session_history_owned', 'wisp_traffic_history_owned')
    fields = ('acctuniqueid','acctsessionid','username','original_username','nasipaddress','calledstationid',
              'nasportid','framedprotocol','servicetype','acctterminatecause',
              'framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix')
    for table in stores:
        retained = table == 'wisp_traffic_history_owned'
        columns = [f"CONVERT(COALESCE({f},'') USING utf8mb4) COLLATE utf8mb4_bin AS {f}" for f in fields]
        columns += ['radacctid','acctstarttime','acctupdatetime','acctstoptime','acctinputoctets','acctoutputoctets']
        columns += ['acctsessiontime']
        columns += [f"CONVERT({f} USING utf8mb4) COLLATE utf8mb4_bin AS {f}"
                    for f in ('framedipaddress','callingstationid')]
        fallback = 'source_fallback' if retained else f"CASE WHEN COALESCE(acctuniqueid,'')='' AND COALESCE(acctsessionid,'')='' AND acctstarttime IS NULL THEN CONCAT('{table}:',radacctid) ELSE '' END"
        columns += [f"CONVERT({fallback} USING utf8mb4) COLLATE utf8mb4_bin AS fallback_id",
                    f"{1 if table == 'radacct_owned' else 0} AS live_source",
                    f"{1 if table == 'wisp_imported_session_history_owned' else 0} AS imported_history"]
        clauses = ["LOWER(username)=LOWER(%s)", "COALESCE(acctterminatecause,'')<>'Consolidated-Historical-Import'"]
        params.append(username)
        if owner is not None:
            scope, count = owner_scope_sql(table)
            clauses.append(scope)
            params.extend([owner] * count)
        parts.append(f"SELECT {','.join(columns)} FROM {table} WHERE {' AND '.join(clauses)}")
    # Blank fields can be filled by another copy; conflicting evidence stays unknown.
    unique_fields = ('original_username','calledstationid','nasportid','framedprotocol','servicetype','framedipaddress','callingstationid','acctterminatecause',
                     'framedipv6address','framedipv6prefix','framedinterfaceid','delegatedipv6prefix')
    aggregates = [f"CASE WHEN MIN(NULLIF({f},''))=MAX(NULLIF({f},'')) THEN MAX(NULLIF({f},'')) ELSE NULL END AS {f}" for f in unique_fields]
    aggregates += ['MAX(acctupdatetime) AS acctupdatetime','MAX(acctstoptime) AS acctstoptime',
                   'MAX(acctsessiontime) AS acctsessiontime','MAX(live_source) AS live_source',
                   'MIN(radacctid) AS radacctid','MIN(imported_history) AS imported_history']
    aggregates += [f'MAX(CASE WHEN {f}>=0 THEN {f} END) AS {f}' for f in ('acctinputoctets','acctoutputoctets')]
    keys = 'acctuniqueid,acctsessionid,username,nasipaddress,acctstarttime,fallback_id'
    return f"SELECT {keys},{','.join(aggregates)} FROM ({' UNION ALL '.join(parts)}) copies GROUP BY {keys}", tuple(params)


def get_session_history(username, page=1, page_size=50):
    from database.db import get_connection
    from core.ownership import owner_id
    from core.time_service import get_utc_cutoff_str
    sql, params = history_sql(username, owner_id())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            def query(statement, values=()):
                cur.execute(statement.replace('?', '%s'), values)
                return list(cur.fetchall())
            try:
                total = int(query(f'SELECT COUNT(*) n FROM ({sql}) sessions', params)[0]['n'])
                pages = max(1, (total + page_size - 1) // page_size) if page_size else 1
                page = max(1, min(int(page), pages))
                order = ' ORDER BY acctstarttime DESC,acctuniqueid DESC,acctsessionid DESC,username,nasipaddress,fallback_id'
                suffix = ' LIMIT %s OFFSET %s' if page_size else ''
                rows = query(sql + order + suffix, params + ((page_size, (page-1)*page_size) if page_size else ()))
                format_history_rows(rows, query)
                return dict(rows=rows,total=total,page=page,pages=pages)
            finally:
                conn.rollback()


def format_history_rows(rows, query):
    from core.time_service import get_utc_cutoff_str
    enrich_session_display(rows, query)
    cutoff = str(get_utc_cutoff_str(5))
    for row in rows:
        down, up = row['acctoutputoctets'], row['acctinputoctets']
        row['download_str'] = format_bytes(int(down)) if down is not None else 'غير محدد'
        row['upload_str'] = format_bytes(int(up)) if up is not None else 'غير محدد'
        row['total_str'] = format_bytes(int(down)+int(up)) if down is not None and up is not None else 'غير محدد'
        row['duration_str'] = format_duration(row['acctsessiontime']) if row['acctsessiontime'] is not None else 'غير محدد'
        observed = row.get('acctupdatetime') or row.get('acctstarttime')
        row['is_active'] = bool(row['live_source'] and not row['acctstoptime'] and observed and str(observed) >= cutoff)


def iter_session_history(username, batch_size=250):
    """One consistent snapshot for the complete export, with bounded row batches."""
    from database.db import get_connection
    from core.ownership import owner_id
    sql,params=history_sql(username,owner_id())
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                def query(statement, values=()):
                    cur.execute(statement.replace('?', '%s'),values)
                    return list(cur.fetchall())
                total=int(query(f'SELECT COUNT(*) n FROM ({sql}) sessions',params)[0]['n'])
                for offset in range(0,max(total,1),batch_size):
                    rows=query(sql+' ORDER BY acctstarttime DESC,acctuniqueid DESC,acctsessionid DESC,username,nasipaddress,fallback_id LIMIT %s OFFSET %s',params+(batch_size,offset))
                    format_history_rows(rows,query)
                    yield rows,total,offset
        finally:
            conn.rollback()
