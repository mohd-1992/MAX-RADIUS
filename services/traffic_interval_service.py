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


def filters(selected,owner,*,prefix='',aliases=None):
    aliases=aliases or {}
    clauses=[];params=[]
    if owner is not None:clauses.append(prefix+'owner_scope=%s');params.append(int(owner))
    value=selected.get('nas','')
    if value not in ('','all'):clauses.append(prefix+'nas_address=%s');params.append('' if value=='unknown' else value)
    def slots(keys):return ','.join('%s' for _ in keys)
    service=selected.get('service_type','')
    if service not in ('','all'):
        keys=sorted(key for key,target in aliases.items() if target['service_type']==service)
        if service=='unknown':
            clauses.append(prefix+"service_type=''"+((' AND '+prefix+'point_key NOT IN ('+slots(aliases)+')') if aliases else ''));params+=sorted(aliases)
        elif keys:
            clauses.append('('+prefix+'service_type=%s OR '+prefix+'point_key IN ('+slots(keys)+'))');params.extend([service,*keys])
        else:clauses.append(prefix+'service_type=%s');params.append(service)
    point=selected.get('server','')
    if point=='unknown':
        clauses.append(prefix+'point_known=0'+((' AND '+prefix+'point_key NOT IN ('+slots(aliases)+')') if aliases else ''));params+=sorted(aliases)
    elif point not in ('','all'):
        canonical=aliases.get(point,{}).get('server_key',point)
        keys=sorted({canonical,*[key for key,target in aliases.items() if target['server_key']==canonical]})
        clauses.append(prefix+'point_key IN ('+slots(keys)+')');params+=keys
    return clauses,params


def read_intervals(query,start,end,selected,owner,aliases=None):
    clauses=['end_at>%s','start_at<%s'];params=[start,end]
    scoped,values=filters(selected,owner,aliases=aliases);clauses.extend(scoped);params.extend(values)
    return query('SELECT * FROM wisp_traffic_intervals WHERE '+' AND '.join(clauses)+' ORDER BY start_at,end_at',tuple(params))


def contribution(row,start,end):
    contained=row['start_at']>=start and row['end_at']<=end
    return {key:row.get(key) if contained else None for key in ('download','upload')}
