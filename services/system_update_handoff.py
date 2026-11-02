"""Scoped controller handoff after a verified Web-only update. No DB restore."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse

from services.system_update_runtime import UpdateError, inspect, candidate_spec, endpoints_for, restore, write_json

from services.system_update_requirements import ENGINE_PROTOCOL


def freeze(api, config, compose):
    """Freeze one provisioned controller; helpers never match Compose labels."""
    service='update_controller'
    if service not in compose.get('services',{}):return None
    if not config.get('controller_mounts'):raise UpdateError('Controller storage binding unavailable')
    fleet=api('GET','/containers/json?all=1')
    matches=[x['Id'] for x in fleet if x.get('State')=='running'
        and (x.get('Labels') or {}).get('com.docker.compose.project')==config['project']
        and (x.get('Labels') or {}).get('com.docker.compose.service')==service]
    if len(matches)!=1:raise UpdateError('Ambiguous update controller')
    old=inspect(api,matches[0])
    # Reject unsupported networking/storage while Web is still untouched.
    candidate_spec(old, old['Image'], endpoints_for(old))
    result={'service':service,'old':old}
    for destination in ('/max-update-private','/max-update-socket','/max-update-compose','/var/run/docker.sock'):
        mounts=[m for m in old['Mounts'] if m['Destination']==destination and m.get('RW')]
        expected=[m for m in config['controller_mounts'] if m['Target']==destination]
        actual_source=mounts[0].get('Name') if len(mounts)==1 and mounts[0]['Type']=='volume' else mounts[0].get('Source') if len(mounts)==1 else None
        if destination=='/max-update-socket' and len(mounts)==1 and not expected:
            # Older provisioning omitted this item from helper configuration.
            # Recover it only from the already shared, read-only owned Web mount.
            webs=[inspect(api,x['Id']) for x in fleet if x.get('State')=='running'
                and (x.get('Labels') or {}).get('com.docker.compose.project')==config['project']
                and (x.get('Labels') or {}).get('com.docker.compose.service')==config['services']['web']]
            shared=[m for w in webs for m in w.get('Mounts',[]) if m['Destination']==destination
                and not m.get('RW') and m['Type']==mounts[0]['Type']
                and (m.get('Name') if m['Type']=='volume' else m.get('Source'))==actual_source]
            if len(webs)!=1 or len(shared)!=1:raise UpdateError('Legacy controller socket is not shared with owned Web')
            result['legacy_socket_mount']={'Type':mounts[0]['Type'],'Source':actual_source,
                'Target':destination,'ReadOnly':False}
            continue
        if len(mounts)!=1 or len(expected)!=1 or actual_source!=expected[0]['Source']:
            raise UpdateError('Controller mount identity does not match enrollment')
    return result


def wait_ready(api, ident, plan, project, previous=False, clock=time.monotonic, sleep=time.sleep):
    from services.system_update_executor import execute
    code=r'''import json,hashlib,pathlib,subprocess,http.client,socket,sys
class Unix(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX);self.sock.settimeout(3);self.sock.connect('/max-update-socket/controller.sock')
c=Unix('localhost');c.request('GET','/_max_updates/v1/progress' if sys.argv[1]=='previous' else '/_max_updates/v1/engine');r=c.getresponse();v=json.loads(r.read());c.close()
v['verified']=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True).returncode==0
v['publisher_key']=hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest()
print(json.dumps(v))'''
    deadline=clock()+90
    while clock()<deadline:
        try:
            value=inspect(api,ident)
            if value['State']['Running']:
                result=execute(api,ident,code,args=('previous' if previous else 'candidate',),timeout=8)
                identity_ok='status' in result if previous else result.get('engine_protocol')==ENGINE_PROTOCOL and result.get('project')==project
                if identity_ok and result.get('verified') is True and result.get('publisher_key')==plan['publisher_key']:return
        except (UpdateError,OSError,KeyError,ValueError):pass
        sleep(1)
    raise UpdateError('New update controller did not verify')


def persist(plan, folder, rollback=False):
    import yaml
    path=Path(plan['compose_path']);before=(folder/'controller-compose-before.private').read_bytes()
    document=yaml.safe_load(before)
    document['services'][plan['controller']['service']]['image']=plan['references']['web']
    updated=yaml.safe_dump(document,sort_keys=False,allow_unicode=True).encode()
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).digest() not in (
            hashlib.sha256(before).digest(),hashlib.sha256(updated).digest()):
        raise UpdateError('Compose changed during controller handoff')
    if plan.get('schema_preparation') and not rollback:
        from services.system_update_sequence import persist_schema
        persist_schema(plan,folder)
    temp=path.with_name('.'+path.name+'.controller-'+plan['job'])
    with temp.open('wb') as out:
        os.chmod(temp,0o600);out.write(before if rollback else updated);out.flush();os.fsync(out.fileno())
    os.replace(temp,path)


def run(folder, api, wait=None):
    """Never replay an uncertain handoff, including after helper/host interruption."""
    import fcntl
    wait=wait or wait_ready;folder=Path(folder)
    with (folder/'controller-execution.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=json.loads((folder/'plan.private.json').read_text())
        state=json.loads((folder/'state.json').read_text());journal=folder/'controller-handoff.private.json'
        if state['status']=='completed' and state.get('engine_protocol')==ENGINE_PROTOCOL:return state
        if state['status']!='handoff_pending':return state
        if journal.exists():
            state.update(status='recovery_required',stage='انقطع تسليم محرك التحديث؛ راجع الإيصال قبل أي إعادة')
            write_json(folder/'state.json',state);return state
        old=plan['controller']['old'];record={};touched=False
        try:
            # The controller always uses the exact already-verified candidate Web.
            if set(plan['images'])!={'web'}:raise UpdateError('Controller preparation must be Web-only')
            web=inspect(api,state['services']['web']['new_id'])
            if web['Image']!=plan['images']['web'] or not web['State']['Running']:raise UpdateError('Verified Web identity changed')
            actual=inspect(api,old['Id'])
            if any(actual[k]!=old[k] for k in ('Id','Image','Name','Config','HostConfig','Mounts')) or actual['State']['StartedAt']!=old['State']['StartedAt']:
                raise UpdateError('Controller identity changed')
            mode=old['HostConfig'].get('NetworkMode')
            endpoints=endpoints_for(old)
            spec=candidate_spec(old,plan['images']['web'],endpoints)
            if mode=='none':spec.pop('NetworkingConfig',None)
            spec.setdefault('Labels',{})['max.update.engine.job']=plan['job']
            # restore() validates the standard job label before touching a candidate.
            spec['Labels']['max.update.job']=plan['job']
            current=Path(plan['compose_path'])
            if current.is_symlink():raise UpdateError('Compose symlink rejected')
            (folder/'controller-compose-before.private').write_bytes(current.read_bytes())
            record.update(attempted=True,old_id=old['Id']);write_json(journal,record)
            touched=True
            api('POST','/containers/'+old['Id']+'/update',{'RestartPolicy':{'Name':'no'}})
            api('POST','/containers/'+old['Id']+'/stop?t=20')
            for net in endpoints:api('POST','/networks/'+urllib.parse.quote(net,safe='')+'/disconnect',{'Container':old['Id'],'Force':True})
            api('POST','/containers/'+old['Id']+'/rename?name='+old['Name'].lstrip('/')+'_previous_engine_'+plan['job'])
            record['create_attempted']=True;write_json(journal,record)
            created=api('POST','/containers/create?name='+old['Name'].lstrip('/'),spec)
            record['new_id']=created['Id'];write_json(journal,record)
            api('POST','/containers/'+created['Id']+'/start')
            wait(api,created['Id'],plan,plan['project'])
            if plan.get('schema_preparation'):
                from services.system_update_sequence import BASELINE_PROBE
                from services.system_update_executor import execute
                proof=execute(api,web['Id'],BASELINE_PROBE)
                if proof!=plan['schema_preparation']:raise UpdateError('New bridge schema did not verify')
            persist(plan,folder)
            api('POST','/containers/'+created['Id']+'/update',{'RestartPolicy':old['HostConfig']['RestartPolicy']})
            record.update(verified=True,protocol=ENGINE_PROTOCOL);write_json(journal,record)
            state.update(status='completed',percent=100,engine_protocol=ENGINE_PROTOCOL,
                stage='اكتمل تحديث Web وتسليم محرك التحديث بعد التحقق',completed_at=time.time())
        except Exception as error:
            record['failure_type']=type(error).__name__
            if touched:
                try:
                    if record.get('create_attempted') and not record.get('new_id'):
                        fleet=api('GET','/containers/json?all=1')
                        found=[x['Id'] for x in fleet if old['Name'] in x.get('Names',[]) and x['Id']!=old['Id']]
                        if len(found)>1:raise UpdateError('Ambiguous controller create outcome')
                        if found:record['new_id']=found[0]
                    restore(api,{'job':plan['job'],'role':'update_controller','old':old,'target_id':plan['images']['web'],
                        'endpoints':endpoints},record)
                    # Read-only proof of the retained controller, not a fake protocol2 claim.
                    value=inspect(api,old['Id'])
                    if not value['State']['Running']:raise UpdateError('Previous controller unavailable')
                    wait(api,old['Id'],plan,plan['project'],previous=True)
                    persist(plan,folder,rollback=True)
                    record['previous_running']=True
                except Exception as recovery:record['recovery_error']=type(recovery).__name__
            write_json(journal,record)
            state.update(status='recovery_required',percent=95,
                stage='Web محدثة لكن تهيئة المحرك لم تكتمل؛ راجع إيصال الاسترداد')
        write_json(folder/'state.json',state);return state
