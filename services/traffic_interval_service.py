"""Keep delayed counter differences without guessing which side of a date owns them."""
import hashlib


def save_interval(cur,key,before,after,point,owner,download,upload,reason):
    identity=hashlib.sha256((key+'|'+str(before['observed_at'])+'|'+str(after)).encode()).hexdigest()
    cur.execute('''INSERT INTO wisp_traffic_intervals
        (interval_key,start_at,end_at,owner_scope,point_key,nas_address,service_type,server_name,server_interface,point_known,download,upload,reason)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE interval_key=interval_key''',
        (identity,before['observed_at'],after,owner,point['server_key'],point['nas_address'],point['service_type'],
         point.get('server_name',''),point.get('server_interface',''),int(point['point_known']),download,upload,reason))


def filters(selected,owner,*,prefix=''):
    clauses=[];params=[]
    if owner is not None:clauses.append(prefix+'owner_scope=%s');params.append(int(owner))
    for field,column in [('nas','nas_address'),('service_type','service_type')]:
        value=selected.get(field,'')
        if value not in ('','all'):clauses.append(prefix+column+'=%s');params.append('' if value=='unknown' else value)
    point=selected.get('server','')
    if point=='unknown':clauses.append(prefix+'point_known=0')
    elif point not in ('','all'):clauses.append(prefix+'point_key=%s');params.append(point)
    return clauses,params


def read_intervals(query,start,end,selected,owner):
    clauses=['end_at>%s','start_at<%s'];params=[start,end]
    scoped,values=filters(selected,owner);clauses.extend(scoped);params.extend(values)
    return query('SELECT * FROM wisp_traffic_intervals WHERE '+' AND '.join(clauses)+' ORDER BY start_at,end_at',tuple(params))


def contribution(row,start,end):
    contained=row['start_at']>=start and row['end_at']<=end
    return {key:row.get(key) if contained else None for key in ('download','upload')}
