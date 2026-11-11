"""Pilot read-only CPU metrics; the installed unit is pinned to max1 only."""
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote,urlsplit

import factory_fleet_proxy as legacy
LEGACY_RESPOND=legacy.respond

_LOCK=threading.Lock()
_CACHE={}
ROLES={'web','radius_core','db'}

def collect(tenant,backend=legacy.docker_read,now=time.time):
    filters=quote(json.dumps({'label':['com.docker.compose.project='+tenant]},separators=(',',':')))
    items=backend('/containers/json?all=0&filters='+filters)
    owned={}
    for item in items:
        labels=item.get('Labels',{});role=labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project')!=tenant or role not in ROLES:continue
        if role in owned:raise ValueError('Duplicate owned role')
        owned[role]=item
    if set(owned)!=ROLES:raise ValueError('Owned services unavailable')
    def sample(pair):
        role,item=pair;ident=item['Id']
        if not re.fullmatch('[a-f0-9]{64}',ident):raise ValueError('Invalid container identity')
        def verify():
            v=backend('/containers/'+ident+'/json');labels=v.get('Config',{}).get('Labels',{})
            if (v.get('Id')!=ident or not v.get('State',{}).get('Running') or labels.get('com.docker.compose.project')!=tenant
                    or labels.get('com.docker.compose.service')!=role or labels.get('max.panel.shared')=='true'):
                raise ValueError('Container ownership changed')
        verify();v=backend('/containers/'+ident+'/stats?stream=false');verify()
        current=v['cpu_stats'];previous=v['precpu_stats'];system=current['system_cpu_usage']-previous['system_cpu_usage']
        used=current['cpu_usage']['total_usage']-previous['cpu_usage']['total_usage']
        if previous['system_cpu_usage']<=0 or system<=0 or used<0:raise ValueError('CPU sample is not ready')
        percent=used/system*100.0
        if not math.isfinite(percent) or not 0<=percent<=100:raise ValueError('Invalid CPU sample')
        return role,percent
    with ThreadPoolExecutor(max_workers=3) as executor:values=dict(executor.map(sample,owned.items()))
    return {'schema':1,'tenant':tenant,'cpu_available':True,'cpu_percent':round(min(100,sum(values.values())),2),'cpu_basis':'host_capacity',
            'sampled_at':now(),'services':{k:{'cpu_percent':round(v,2)} for k,v in values.items()}}

def respond(tenant,method,path,backend=legacy.docker_read):
    url=urlsplit(path)
    if url.path!='/_max_metrics/v1/resources':return LEGACY_RESPOND(tenant,method,path,backend)
    if tenant!='max1' or method!='GET' or url.query:return 403,{'message':'Scoped read-only metric required'}
    try:
        with _LOCK:
            cached=_CACHE.get(tenant)
            if cached and 0<=time.time()-cached['sampled_at']<3:return 200,cached
            value=collect(tenant,backend);_CACHE[tenant]=value;return 200,value
    except Exception:return 503,{'cpu_available':False,'cpu_percent':None,'message':'تعذر قراءة استهلاك خدمات الدومين'}

def main():
    if len(sys.argv)!=2 or sys.argv[1]!='max1':raise SystemExit('This pilot serves max1 only')
    legacy.respond=respond
    legacy.main()

if __name__=='__main__':main()
