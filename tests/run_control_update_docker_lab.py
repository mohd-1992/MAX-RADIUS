"""Explicit isolated Docker lifecycle acceptance; no customer, NAS or real license.

Uses a new MariaDB and synthetic counters for each case. Runtime/license probes
are deliberately injected: this tests switching/recovery, not licensed RADIUS.
Run inside the candidate with its Docker socket and a NEW /work report directory.
"""
import copy
import json
import os
from pathlib import Path
import sys
import time
import uuid

from services.system_update_runtime import Docker, inspect, write_json, UpdateError
from services.system_update_executor import run_job


def main():
    if sys.argv[1:] != ['--run']: raise SystemExit('Explicit --run required')
    root=Path('/work')
    if (root/'ATTEMPTED').exists(): raise SystemExit('Do not replay a completed or uncertain lab')
    (root/'ATTEMPTED').write_text('Fresh isolated lifecycle acceptance')
    api=Docker();base='sha256:68a3daa26878fdb18dfc0ba70f128261093af1ead8e0db99c06ba7490f7d914f'
    candidate=api('GET','/images/mohd777%2Fmax-radius-web%3A20261022-v2.6.4-control-v1/json')['Id']
    prefix='max264lab'+uuid.uuid4().hex[:8]
    previous=api('GET','/containers/json?all=1')
    frozen={v['Id']:{k:inspect(api,v['Id'])[k] for k in ('Image','Mounts')} for v in previous}
    created=[];results=[]
    def execsql(ident,sql):
        task=api('POST','/containers/'+ident+'/exec',{'AttachStdout':True,'AttachStderr':True,'Tty':True,
                 'Cmd':['mariadb','-uroot','-pfixture-only','-N','-B','-e',sql]})
        result=api('POST','/exec/'+task['Id']+'/start',{'Detach':False,'Tty':True})
        if api('GET','/exec/'+task['Id']+'/json')['ExitCode']:raise UpdateError('Fixture SQL failed')
        return result
    try:
        cases=os.environ.get('MAX_UPDATE_LAB_CASES','success,rollback,lost_create').split(',')
        if not cases or set(cases)-{'success','rollback','lost_create'} or len(set(cases))!=len(cases):raise ValueError('Invalid lab case selection')
        for case in cases:
            project=prefix+'_'+case
            # Existing developer networks may exhaust Docker's default pools.
            subnet='10.239.'+str(int(uuid.uuid4().hex[:2],16))+'.0/28'
            network=api('POST','/networks/create',{'Name':project,'Internal':True,
                        'IPAM':{'Config':[{'Subnet':subnet}]}})['Id']
            old={}
            for role in ('db','core','web'):
                value={'Image':'mariadb:10.11' if role=='db' else base,
                       'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':role},
                       'HostConfig':{'RestartPolicy':{'Name':'no'}},'NetworkingConfig':{'EndpointsConfig':{project:{}}}}
                if role=='db':value['Env']=['MARIADB_ROOT_PASSWORD=fixture-only']
                else:value.update(Entrypoint=['python','-c'],Cmd=['import time;time.sleep(900)'],StopSignal='SIGKILL')
                ident=api('POST','/containers/create?name='+project+'_'+role,value)['Id'];created.append(ident)
                api('POST','/containers/'+ident+'/start');old[role]=ident
            ready=False
            for _ in range(60):
                try:
                    result=execsql(old['db'],"SELECT JSON_OBJECT('ready',1)")
                    if result.get('ready')==1:ready=True;break
                except Exception:time.sleep(1)
            if not ready:raise UpdateError('Fresh fixture database did not start')
            execsql(old['db'],"CREATE DATABASE synthetic;CREATE TABLE synthetic.radacct(radacctid BIGINT PRIMARY KEY,acctinputoctets BIGINT,acctoutputoctets BIGINT);INSERT INTO synthetic.radacct VALUES(1,4294967400,8589934600);SELECT JSON_OBJECT('created',1)")
            snapshots={r:inspect(api,i) for r,i in old.items()}
            job=uuid.uuid4().hex
            folder=root/case/job;folder.mkdir(parents=True)
            compose=folder/'compose.json';compose.write_text(json.dumps({'services':{r:{'image':snapshots[r]['Image']} for r in old}}))
            (folder/'compose-before.private').write_bytes(compose.read_bytes())
            plan={'job':job,'old':snapshots,'images':{'web':candidate,'core':candidate},
                  'references':{'web':'mohd777/max-radius-web@sha256:'+'a'*64,'core':'mohd777/max-radius-core@sha256:'+'b'*64},
                  'version':'2.6.5','previous_version':'2.6.4','publisher_key':'fixture-probe-only','accounting':[],
                  'compose_path':str(compose),'compose_services':{'web':'web','core':'core','db':'db'}}
            write_json(folder/'plan.private.json',plan);write_json(folder/'state.json',{'status':'queued'})
            calls=[];lost=[False]
            def traced(method,path,body=None,timeout=30):
                calls.append([method,path])
                result=api(method,path,body,timeout)
                if method=='POST' and path.startswith('/containers/create?'):
                    created.append(result['Id'])
                    if case=='lost_create' and not lost[0]:lost[0]=True;raise UpdateError('Injected lost create response')
                return result
            def probe(_api,ident,role,_plan,rollback=False):
                if not inspect(api,ident)['State']['Running']:raise UpdateError('Fixture is not running')
                # Simulate real counter progress while replacement processes run.
                execsql(old['db'],"UPDATE synthetic.radacct SET acctinputoctets=acctinputoctets+100,acctoutputoctets=acctoutputoctets+200 WHERE radacctid=1;SELECT JSON_OBJECT('advanced',1)")
                if case=='rollback' and role=='web' and not rollback:raise UpdateError('Injected candidate probe failure')
            state=run_job(folder,traced,probe)
            expected='completed' if case=='success' else 'rolled_back'
            if state['status']!=expected:raise UpdateError('Unexpected lifecycle result: '+state['status'])
            before_calls=len(calls);assert run_job(folder,traced,probe)['status']==expected and len(calls)==before_calls
            after=execsql(old['db'],"SELECT JSON_OBJECT('rows',COUNT(*),'input',MIN(acctinputoctets),'output',MIN(acctoutputoctets)) FROM synthetic.radacct")
            assert after['rows']==1 and after['input']>=4294967400 and after['output']>=8589934600
            db=inspect(api,old['db']);assert db['State']['StartedAt']==snapshots['db']['State']['StartedAt'] and db['Image']==snapshots['db']['Image']
            allowed=set(created)
            for method,path in calls:
                if method!='GET' and path.startswith('/containers/') and not path.startswith('/containers/create?'):
                    assert path.split('/')[2] in allowed
            results.append({'case':case,'status':state['status'],'database_unchanged':True,'synthetic_64bit_counters_preserved':True,
                            'repeat_noop':True,'runtime_license_probe':'injected-not-production'})
        for ident,value in frozen.items():
            actual=inspect(api,ident)
            assert all(actual[k]==v for k,v in value.items())
        write_json(root/'RESULTS.private.json',{'passed':True,'cases':results,'preexisting_containers_unchanged':len(frozen),
                   'real_nas_or_license_acceptance':False})
        print(json.dumps({'passed':True,'cases':results,'preexisting_containers_unchanged':len(frozen)}))
    finally:
        for ident in set(created):
            try:
                value=inspect(api,ident)
                if value['State']['Running']:api('POST','/containers/'+ident+'/stop?t=5')
                api('POST','/containers/'+ident+'/update',{'RestartPolicy':{'Name':'no'}})
            except Exception:pass


if __name__=='__main__':main()
