"""Fixed signed-image schema plan. No SQL or executable text from a feed."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse

from services.system_update_runtime import Docker, UpdateError, inspect, candidate_spec, endpoints_for, write_json

PLAN_PATH = Path(__file__).resolve().parents[1] / 'database/update_plans/features268.json'
BUSINESS = ('wisp_subscribers','wisp_packages','wisp_vouchers','wisp_voucher_sales',
            'wisp_invoices','radacct','radcheck','radreply','radgroupcheck','radgroupreply')


def metadata():
    raw = PLAN_PATH.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def validate_plan(value, components):
    spec, digest = metadata()
    if value != {'id': spec['id'], 'sha256': digest} or set(components) != set(spec['components']):
        raise ValueError('Unknown immutable schema plan or component set')
    return spec


def initialize():
    """Called only in the signed candidate's isolated migration process."""
    import threading
    threading.Thread.start = lambda self: None
    from database.db import get_connection, init_database, query_all, query_one
    def checkpoint(columns=None):
        result = {}
        for table in BUSINESS:
            key = 'radacctid' if table == 'radacct' else 'id'
            fields = columns[table]['columns'] if columns else [r['Field'] for r in query_all('SHOW COLUMNS FROM `'+table+'`') if r['Field'] != 'global_seq_id']
            if not fields or not all(v.replace('_','').isalnum() for v in fields):
                raise ValueError('Invalid schema checkpoint columns')
            digest = hashlib.sha256(); count = 0; last = -1
            while True:
                batch = query_all('SELECT '+','.join('`'+v+'`' for v in fields)+' FROM `'+table+'` WHERE `'+key+'`>%s ORDER BY `'+key+'` LIMIT 1000',(last,))
                if not batch: break
                for row in batch:
                    digest.update(json.dumps(row,sort_keys=True,separators=(',',':'),default=str).encode()+b'\n')
                    count += 1
                last = batch[-1][key]
            result[table] = {'columns':fields,'rows':count,'sha256':digest.hexdigest()}
        return result
    # Dedicated session lock survives implicit DDL commits and never targets another DB.
    with get_connection() as lock:
        with lock.cursor() as cur:
            cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(),':max-features268'),10) AS acquired")
            if cur.fetchone()['acquired'] != 1: raise RuntimeError('Schema migration lock unavailable')
        try:
            before = checkpoint()
            init_database()
            after = checkpoint(before)
            if after != before: raise RuntimeError('Existing business fields changed during schema initialization')
            for table, entity in [('wisp_subscribers','subscriber'),('wisp_vouchers','voucher')]:
                missing = query_one('SELECT COUNT(*) n FROM `'+table+'` a LEFT JOIN wisp_global_sequence g ON g.entity_type=%s AND g.entity_id=a.id WHERE a.global_seq_id IS NULL OR g.seq_id IS NULL OR a.global_seq_id<>g.seq_id',(entity,))
                if missing['n']: raise RuntimeError('Global identity backfill did not verify')
            required = ('wisp_account_mac_policies','wisp_account_mac_addresses','wisp_account_mac_receipts','wisp_mac_radcheck')
            for name in required:
                if not query_one('SELECT 1 n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(name,)):
                    raise RuntimeError('Required MAC schema absent')
            functions = query_all("SELECT ROUTINE_NAME,ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME IN ('fn_account_mac_allowed','fn_reserve_license_slot')")
            if len(functions)!=2 or not any('fn_account_mac_allowed' in (r['ROUTINE_DEFINITION'] or '') for r in functions if r['ROUTINE_NAME']=='fn_reserve_license_slot'):
                raise RuntimeError('MAC reservation enforcement absent')
            return {'verified':True,'schema_revision':'features268','business':after,'database_restored':False}
        finally:
            with lock.cursor() as cur: cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(),':max-features268'))")


ATTEST = """import hashlib,json,pathlib,subprocess
p=pathlib.Path('/app/database/update_plans/features268.json')
print(json.dumps({'verified':subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True).returncode==0,'publisher_key':hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest(),'plan_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))
"""


def attest(api, image, job, expected_key):
    from services.system_update_executor import execute
    spec={'Image':image,'Entrypoint':['python','-c'],'Cmd':['import time;time.sleep(120)'],
          'Labels':{'max.update.artifact':job,'autoheal':'false'},
          'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'RestartPolicy':{'Name':'no'},'Tmpfs':{'/tmp':'rw,size=32m'}}}
    name='max_artifact_'+job+'_'+image.split(':')[-1][:10]
    created=api('POST','/containers/create?name='+name,spec)['Id']
    try:
        api('POST','/containers/'+created+'/start')
        result=execute(api,created,ATTEST)
        if not result.get('verified') or result.get('publisher_key')!=expected_key or result.get('plan_sha256')!=metadata()[1]:
            raise UpdateError('Migration/rollback artifact did not attest')
    finally:
        # Delete exactly the ephemeral artifact reader created in this call.
        api('DELETE','/containers/'+created+'?force=1&v=1')


def attach(api, manifest, plan):
    requirements=manifest.get('requirements') or {}
    spec=validate_plan(requirements.get('migration_plan'),manifest['images'])
    if manifest['version']!=spec['version'] or plan['previous_version']!='2.6.7':
        raise UpdateError('Schema plan version boundary does not match')
    rollback=requirements['rollback']
    if rollback['policy']!='compatible' or set(rollback['images'])!={'web','core'}:
        raise UpdateError('A compatible rollback pair is mandatory')
    ids={}
    for role, reference in rollback['images'].items():
        api('POST','/images/create?fromImage='+urllib.parse.quote(reference,safe=''),timeout=600)
        value=api('GET','/images/'+urllib.parse.quote(reference,safe='')+'/json')
        if reference not in value.get('RepoDigests',[]): raise UpdateError('Rollback digest did not verify')
        ids[role]=value['Id']
    attest(api,plan['images']['web'],plan['job'],plan['publisher_key'])
    attest(api,ids['web'],plan['job'],plan['publisher_key'])
    plan['migration']={'spec':spec,'sha256':metadata()[1]}
    plan['rollback_images']=ids; plan['rollback_references']=rollback['images']


def create_once(api, folder, state, role, spec, name, key):
    record=state.setdefault(key,{})
    if record.get(role): raise UpdateError('Create intent already exists; reconcile instead of replay')
    record[role]={'create_attempted':True};write_json(folder/'state.json',state)
    try:
        ident=api('POST','/containers/create?name='+urllib.parse.quote(name,safe=''),spec)['Id']
    except Exception:
        rows=[r for r in api('GET','/containers/json?all=1') if '/'+name in r.get('Names',[]) and (r.get('Labels') or {}).get('max.update.job')==state['job']]
        if len(rows)==1:record[role]['id']=rows[0]['Id'];write_json(folder/'state.json',state)
        raise
    record[role]['id']=ident;write_json(folder/'state.json',state)
    return ident


def stop_detach(api, value, name):
    networks=endpoints_for(value)
    api('POST','/containers/'+value['Id']+'/update',{'RestartPolicy':{'Name':'no'}})
    if value['State']['Running']:api('POST','/containers/'+value['Id']+'/stop?t=25')
    for net in networks:
        api('POST','/networks/'+urllib.parse.quote(net,safe='')+'/disconnect',{'Container':value['Id'],'Force':True})
    api('POST','/containers/'+value['Id']+'/rename?name='+urllib.parse.quote(name,safe=''))


def run_initializer(api, plan, folder, state):
    from services.system_update_executor import execute
    marker=folder/'schema-migration.private.json'
    if marker.exists(): raise UpdateError('An attempted schema migration is never replayed automatically')
    write_json(marker,{'attempted':True,'plan_sha256':plan['migration']['sha256']})
    old=plan['old']['web'];nets=list(old['NetworkSettings']['Networks'])
    if len(nets)!=1:raise UpdateError('Migration requires a single owned DB network')
    env=[v for v in old['Config'].get('Env',[]) if not v.startswith(('HOSTNAME=','STORAGE_DIR=','BACKUPS_DIR=','MAX_UPDATE_SOCKET='))]
    env+=['STORAGE_DIR=/tmp/migration-storage','PYTHONDONTWRITEBYTECODE=1']
    spec={'Image':plan['images']['web'],'Entrypoint':['python','-c'],'Cmd':['import time;time.sleep(600)'],'Env':env,
          'Labels':{'max.update.job':plan['job'],'autoheal':'false'},
          'HostConfig':{'NetworkMode':nets[0],'ReadonlyRootfs':True,'RestartPolicy':{'Name':'no'},'Tmpfs':{'/tmp':'rw,size=64m'}}}
    ident=create_once(api,folder,state,'schema',spec,'max_schema_'+plan['job'],'migration_services')
    try:
        api('POST','/containers/'+ident+'/start')
        code="""import contextlib,io,json
from services.system_update_migrations import initialize
with contextlib.redirect_stdout(io.StringIO()):
 result=initialize()
print(json.dumps(result))
"""
        result=execute(api,ident,code,timeout=300)
        if result.get('verified') is not True:raise UpdateError('Schema result not verified')
        write_json(marker,result)
        return result
    finally:
        api('POST','/containers/'+ident+'/stop?t=5')


def persist(plan, folder, rollback=False):
    import yaml
    path=Path(plan['compose_path']);before=(folder/'compose-before.private').read_bytes()
    if path.is_symlink() or hashlib.sha256(before).hexdigest()!=plan['compose_sha256']:
        raise UpdateError('Deployment definition changed')
    variants=[]
    for references in (plan['references'],plan['rollback_references']):
        document=yaml.safe_load(before.decode('utf-8'))
        for role,reference in references.items():
            document['services'][plan['compose_services'][role]]['image']=reference
        variants.append(yaml.safe_dump(document,sort_keys=False,allow_unicode=True).encode('utf-8'))
    allowed={hashlib.sha256(value).hexdigest() for value in [before,*variants]}
    if hashlib.sha256(path.read_bytes()).hexdigest() not in allowed:
        raise UpdateError('Deployment definition was changed by another operator')
    temporary=path.with_name('.'+path.name+'.schema-'+plan['job'])
    with temporary.open('wb') as out:
        os.chmod(temporary,0o600);out.write(variants[1 if rollback else 0]);out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    config_path=folder.parent.parent/'update-channel.private.json'
    config=json.loads(config_path.read_text())
    if config['project']!=plan['project']:raise UpdateError('Schema stamp owner changed')
    config['schema_revision']='features268';write_json(config_path,config)


def run(folder, api=None, wait=None):
    import fcntl
    from services.system_update_executor import wait_ready
    api,folder=api or Docker(),Path(folder);wait=wait or wait_ready
    with (folder/'execution.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=json.loads((folder/'plan.private.json').read_text());state=json.loads((folder/'state.json').read_text())
        if state['status'] in ('completed','rolled_back','error','recovery_required'):return state
        if state.get('switch_attempted'):
            state.update(status='recovery_required',stage='انقطع الترحيل؛ راجع الإيصال دون إعادة التنفيذ');write_json(folder/'state.json',state);return state
        if set(plan['images'])!={'web','core'} or not plan.get('rollback_images'):raise UpdateError('Explicit migration needs both component and rollback pairs')
        for role in ('web','core'):
            old=plan['old'][role];actual=inspect(api,old['Id'])
            if any(actual[k]!=old[k] for k in ('Image','Config','HostConfig','Mounts')) or actual['State']['StartedAt']!=old['State']['StartedAt']:raise UpdateError('Component identity changed')
            # Validate both directions before stopping services or running DDL.
            endpoints=endpoints_for(old)
            candidate_spec(old,plan['images'][role],endpoints)
            candidate_spec(old,plan['rollback_images'][role],endpoints)
        state['job']=plan['job'];state['switch_attempted']=True
        state.update(status='running',percent=50,stage='إيقاف المكونات المحددة وترحيل المخطط مع حفظ البيانات');write_json(folder/'state.json',state)
        try:
            for role in ('web','core'):
                state.setdefault('stopped',[]).append(role);write_json(folder/'state.json',state)
                stop_detach(api,inspect(api,plan['old'][role]['Id']),plan['old'][role]['Name'].lstrip('/')+'_previous_'+plan['job'])
            run_initializer(api,plan,folder,state)
            state.update(percent=70,stage='تحقق المخطط؛ تشغيل Web وتجديد الترخيص ثم Core');write_json(folder/'state.json',state)
            # SQL capacity initialization invalidates the previous lease. Verify the
            # real publisher license from persistent Web storage before starting Core.
            for role in ('web','core'):
                old=plan['old'][role];spec=candidate_spec(old,plan['images'][role],endpoints_for(old));spec.setdefault('Labels',{})['max.update.job']=plan['job']
                ident=create_once(api,folder,state,role,spec,old['Name'].lstrip('/'),'migration_candidates')
                api('POST','/containers/'+ident+'/start');wait(api,ident,role,plan)
                api('POST','/containers/'+ident+'/update',{'RestartPolicy':old['HostConfig']['RestartPolicy']})
            persist(plan,folder)
            state.update(status='completed',percent=100,engine_protocol=3,stage='اكتمل الترحيل وتحديث المكونات مع حفظ البيانات',completed_at=time.time())
        except Exception as error:
            write_json(folder/'migration-failure.private.json',{'type':type(error).__name__})
            try:
                # All stopped services come back as the attested compatible pair, even after partial DDL.
                for role in ('web','core'):
                    record=state.get('migration_candidates',{}).get(role,{})
                    if record.get('id'):stop_detach(api,inspect(api,record['id']),'max_'+role+'_failed_'+plan['job'])
                    if role not in state.get('stopped',[]):
                        stop_detach(api,inspect(api,plan['old'][role]['Id']),plan['old'][role]['Name'].lstrip('/')+'_previous_'+plan['job'])
                for role in ('web','core'):
                    old=plan['old'][role];spec=candidate_spec(old,plan['rollback_images'][role],endpoints_for(old));spec.setdefault('Labels',{})['max.update.job']=plan['job']
                    ident=create_once(api,folder,state,role,spec,old['Name'].lstrip('/'),'migration_rollback')
                    api('POST','/containers/'+ident+'/start');wait(api,ident,role,plan,rollback=True)
                    api('POST','/containers/'+ident+'/update',{'RestartPolicy':old['HostConfig']['RestartPolicy']})
                persist(plan,folder,rollback=True)
                state.update(status='rolled_back',percent=0,stage='شغلت صور الرجوع المتوافقة دون استعادة قاعدة البيانات',completed_at=time.time())
            except Exception as recovery:
                write_json(folder/'migration-recovery.private.json',{'type':type(recovery).__name__})
                state.update(status='recovery_required',percent=0,stage='يلزم تحقق الاسترداد؛ لا تعِد الترحيل أو تستعد قاعدة قديمة')
        write_json(folder/'state.json',state);return state
