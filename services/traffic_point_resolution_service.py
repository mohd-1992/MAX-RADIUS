"""Resolve a named report point without rewriting historical packet evidence.

Only a single evidenced Hotspot namespace on the same NAS may absorb its
untyped predecessor. Conflicting protocols and PPP interfaces stay separate.
"""
from services.sales_source_service import server_key

DIMENSIONS=('server_key','nas_address','service_type','server_name','server_interface','point_known')


def aliases_from_points(points):
    namespaces={};blocked=set()
    for point in points:
        nas=point.get('nas_address') or '';name=point.get('server_name') or ''
        kind=point.get('service_type') or '';interface=point.get('server_interface') or ''
        if nas and name and kind in ('pppoe','conflict'):blocked.add((nas,name))
        expected=server_key(nas,kind,name,interface)
        if (not point.get('point_known') or expected=='unknown' or point.get('server_key')!=expected
                or any(ord(c)<32 or ord(c)==127 for c in nas+name+interface)):continue
        namespaces.setdefault((nas,name),{})[expected]=point
    from services.network_traffic_service import point_info
    aliases={}
    for (nas,name),candidates in namespaces.items():
        if len(candidates)!=1 or (nas,name) in blocked:continue
        point=next(iter(candidates.values()))
        if point['service_type']!='hotspot':continue
        untyped=point_info(dict(nas_address=nas,server_name=name),{})
        if not untyped['nas_address']:continue
        aliases[untyped['server_key']]={key:point[key] for key in DIMENSIONS}
    return aliases


def load_aliases(query,owner=None,nas=None):
    clauses=["service_type IN ('hotspot','pppoe','conflict')"];params=[]
    if owner is not None:clauses.append('owner_scope=%s');params.append(int(owner))
    if nas not in (None,'','all'):clauses.append('nas_address=%s');params.append('' if nas=='unknown' else nas)
    where=' AND '.join(clauses)
    columns='point_key server_key,nas_address,service_type,server_name,server_interface,point_known'
    rows=query('SELECT DISTINCT '+columns+' FROM wisp_traffic_rollups WHERE '+where+
               ' UNION SELECT DISTINCT '+columns+' FROM wisp_traffic_intervals WHERE '+where,tuple(params+params))
    return aliases_from_points(rows)


def resolve(point,aliases):
    target=aliases.get(point.get('server_key'))
    return dict(point,**target) if target else dict(point)


def normalize_selection(selected,aliases):
    result=dict(selected)
    if result.get('server') in aliases:result['server']=aliases[result['server']]['server_key']
    return result


def merge_points(rows,aliases):
    combined={}
    counts=('sessions','gaps','unknown_download','unknown_upload','unknown_sessions','unknown_date','open_sessions')
    for raw in rows:
        point=resolve(raw,aliases);key=point['server_key']
        if key not in combined:combined[key]=point;continue
        current=combined[key]
        for field in counts:
            if field in current or field in point:current[field]=int(current.get(field) or 0)+int(point.get(field) or 0)
        for field in ('download','upload'):
            values=[p[field] for p in (current,point) if p.get(field) is not None]
            if field in current or field in point:current[field]=sum(int(v) for v in values) if values else None
        values=[p['last_observed'] for p in (current,point) if p.get('last_observed') is not None]
        if values:current['last_observed']=max(values)
    return list(combined.values())
