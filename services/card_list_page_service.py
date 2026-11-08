"""Read-only, compact global sort keys before hydrating a card page."""
import datetime
from services.list_sort_service import sort_rows


def remaining_seconds(card, now):
    expiry=card.get('expires_at')
    if isinstance(expiry,str):
        try: expiry=datetime.datetime.fromisoformat(expiry.replace('Z',''))
        except Exception: expiry=None
    if not isinstance(expiry,datetime.datetime): expiry=None
    if card.get('status')=='expired': return 0
    if expiry: return max(0,(expiry-now).total_seconds())
    validity=card.get('validity_value')
    if str(validity or '').isdigit():
        return float(validity)*{'minutes':60,'hours':3600,'days':86400,'months':2592000}.get(card.get('validity_unit'),86400)
    return None


def page_ids(query, where, params, state, offset, limit, now, cutoff, quota_reader, accounting_error):
    rows=query(f'''SELECT v.id,v.username,v.status,v.expires_at,v.first_used_at,
        COALESCE(v.snap_validity_value,p.validity_value) AS validity_value,
        COALESCE(v.snap_validity_unit,p.validity_unit) AS validity_unit
        FROM wisp_vouchers v JOIN wisp_packages p ON v.package_id=p.id
        JOIN wisp_voucher_batches b ON v.batch_id=b.id
        LEFT JOIN wisp_resellers r ON v.reseller_id=r.id
        WHERE {where} ORDER BY v.id DESC''',tuple(params))
    if not rows:return [],None
    usage=None
    key=state['key']
    if key=='days':
        for row in rows:row['sort_remaining_seconds']=remaining_seconds(row,now)
    elif key=='consumption':
        try:usage=quota_reader([r['id'] for r in rows])
        except accounting_error:usage={}
        for row in rows:
            value=usage.get((row.get('username') or '').lower())
            row['sort_used_bytes']=float(value['down_bytes'])+float(value['up_bytes']) if value is not None else None
    else:
        names=[r['username'] for r in rows if r.get('username')]
        active={};last={}
        if names:
            marks=','.join('?' for _ in names)
            sessions=query(f'''SELECT username,framedipaddress,acctsessiontime,acctinputoctets,acctoutputoctets,
                calledstationid,nasipaddress FROM radacct
                WHERE username IN ({marks}) AND acctstoptime IS NULL AND
                ((acctupdatetime IS NOT NULL AND acctupdatetime>=?) OR
                 (acctupdatetime IS NULL AND acctstarttime>=?))''',tuple(names)+(cutoff,cutoff))
            from services.session_display_service import index_live_sessions
            active,_=index_live_sessions(sessions)
            last={r['username'].lower():r for r in query(f'''SELECT a.username,a.acctstoptime,
                a.calledstationid,a.nasipaddress,a.framedipaddress FROM radacct a
                INNER JOIN (SELECT username,MAX(radacctid) AS max_id FROM radacct
                WHERE username IN ({marks}) GROUP BY username) m ON a.radacctid=m.max_id''',tuple(names))}
        for row in rows:
            name=(row.get('username') or '').lower();live=active.get(name);previous=last.get(name)
            row['is_online']=bool(live)
            if live:
                row['ip_address']=live.get('framedipaddress') or '-'
                row['access_point']=live.get('calledstationid') or live.get('nasipaddress') or 'MikroTik Hotspot'
                row['last_seen_str']='متصل الآن'
            else:
                row['ip_address']=(previous.get('framedipaddress') if previous else None) or '-'
                row['access_point']=(previous.get('calledstationid') or previous.get('nasipaddress') if previous else None) or '-'
                seen=previous.get('acctstoptime') if previous else None
                row['last_seen_str']=(seen.strftime('%Y-%m-%d %H:%M') if isinstance(seen,datetime.datetime)
                    else str(seen)[:16]) if seen else str(row['first_used_at'])[:16] if row.get('first_used_at') else 'لم يتصل بعد'
    selected=[r['id'] for r in sort_rows(rows,state)[offset:offset+limit]]
    return selected,usage
