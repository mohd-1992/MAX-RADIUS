"""Host-local incident dispatcher. Default CLI is status, never apply.

Private root contains a checksummed source bundle, pinned target, encrypted
backup verification and typed plans. No image/service/router/time changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from lab_repair import digest

MODULES=('lab_repair','priority_repair','plan_codec','customer_operator','operator_kernel')


def atomic_json(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.next')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        json.dump(value,f,separators=(',',':'));f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    if os.name=='posix':
        fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


class Dispatcher:
    def __init__(self,root,*,lab=False):
        self.root=Path(root).resolve();self.lab=lab
        self.binding=json.loads((self.root/'binding.private.json').read_text())
        if bool(self.binding.get('lab_mode'))!=lab:raise RuntimeError('Host mode mismatch')
        if not lab and str(self.root)!='/var/lib/max-radius-rollout/20261017-clock-record-repair-v1':
            raise RuntimeError('Unexpected incident root')
        self.bundle=json.loads((self.root/'source-manifest.json').read_text())
        for name in (*MODULES,'host_dispatch'):
            p=self.root/'source'/(name+'.py')
            if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=self.bundle['sha256'][name]:
                raise RuntimeError('Source checksum mismatch')
            if not lab and (p.stat().st_uid!=0 or p.stat().st_mode&0o077):
                raise RuntimeError('Source file not protected')
        if not self.bundle.get('operator_tests_passed'):raise RuntimeError('Local operator tests required')

    def event(self,phase,**values):
        event={'phase':phase,'epoch':time.time(),**values}
        folder=self.root/'events';folder.mkdir(mode=0o700,exist_ok=True)
        path=folder/(str(time.time_ns())+'.private.json')
        atomic_json(path,event);atomic_json(self.root/'state.private.json',event)
        return event

    def claim(self,action):
        fd=os.open(self.root/('attempted.'+action+'.private.json'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'action':action,'epoch':time.time()},f);f.flush();os.fsync(f.fileno())

    @staticmethod
    def command(args,*,input=None,timeout=15):
        p=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
        if p.returncode:raise RuntimeError('Pinned host command failed')
        return p.stdout

    def check_host(self):
        b=self.binding
        if not self.lab:
            machine=hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest()
            if machine!=b['machine_sha256']:raise RuntimeError('Machine binding mismatch')
            ntp=self.command(['timedatectl','show','-p','NTPSynchronized','--value']).decode().strip()
            if ntp!='yes':raise RuntimeError('NTP not synchronized')
        for item in b['containers']:
            raw=self.command(['docker','inspect','--format','{{.Id}} {{.Image}} {{.State.StartedAt}} {{.RestartCount}} {{.State.Status}} {{with index .State "Health"}}{{.Status}}{{else}}none{{end}}',item['id']]).decode().split()
            if raw[:4]!=[item['id'],item['image'],item['started'],str(item['restarts'])] or raw[4]!='running':
                raise RuntimeError('Pinned live container changed')
            if not self.lab and raw[5]!='healthy':raise RuntimeError('Live service unhealthy')
        backup=json.loads((self.root/'backup.verified.private.json').read_text())
        if not backup.get('verified_offhost') or backup.get('binding_sha')!=digest(b):
            raise RuntimeError('Verified encrypted backup required')
        if not 0 <= time.time()-backup.get('verified_epoch',0) <= 7200:
            raise RuntimeError('Fresh backup verification required')
        return {k:b[k] for k in ('machine_sha256','web_id','db_id','core_id','web_image','core_image')}

    def kernel(self,action,*,plans=None):
        proof=self.check_host();b=self.binding
        # Read one existing credential reference in memory; never inspect/display a full environment.
        pw=self.command(['docker','exec',b['db_id'],'sh','-c','printf "%s" "${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}"']).decode()
        if not pw:raise RuntimeError('Protected database credential unavailable')
        payload={'action':action,'binding':b,'host_proof':proof,'db_host':b['db_host'],
                 'db_port':b['db_port'],'root_password':pw,'lab':self.lab,'plans':plans or []}
        sources={name:(self.root/'source'/(name+'.py')).read_text() for name in MODULES}
        loader="""import json,sys,types
envelope=json.load(sys.stdin)
for name in ('lab_repair','priority_repair','plan_codec','customer_operator','operator_kernel'):
 m=types.ModuleType(name);sys.modules[name]=m
 exec(compile(envelope['sources'][name],'<clock-incident-'+name+'>','exec'),m.__dict__)
try:result=sys.modules['operator_kernel'].run(envelope['request'])
except sys.modules['lab_repair'].Refused as exc:result={'ok':False,'refused':True,'reason':str(exc)}
except Exception as exc:result={'ok':False,'error_type':type(exc).__name__,'error_code':exc.args[0] if exc.args and isinstance(exc.args[0],int) else None}
print(json.dumps(result,separators=(',',':')))
"""
        output=self.command(['docker','exec','-i',b['web_id'],'python','-c',loader],
            input=json.dumps({'sources':sources,'request':payload}).encode(),timeout=45)
        result=json.loads(output)
        # Keep full plan/receipts private, return only a small caller summary.
        atomic_json(self.root/('kernel-'+action+'.private.json'),result)
        return result

    def run(self,action):
        if action=='status':return self._run(action)
        # One host-local action at a time, including the independent watchdog.
        if os.name=='posix':
            import fcntl
            with (self.root/'action.lock').open('a') as lock:
                os.chmod(self.root/'action.lock',0o600)
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                return self._run(action)
        if not self.lab:raise RuntimeError('Linux host action lock required')
        return self._run(action)

    def _run(self,action):
        if action=='status':
            return json.loads((self.root/'state.private.json').read_text()) if (self.root/'state.private.json').exists() else {'phase':'staged'}
        if action=='prepare':
            self.claim(action)
            result=self.kernel('prepare')
            if not result.get('ok'):return self.event('prepare_refused',result=result)
            atomic_json(self.root/'plans.private.json',result)
            return self.event('prepared',summary=result['summary'],pilot=result['pilot'])
        prepared=json.loads((self.root/'plans.private.json').read_text())
        pilot=prepared['pilot'];entries=prepared['plans']
        phase=self.run('status').get('phase')
        if action=='journal':
            if phase!='prepared':raise RuntimeError('Prepare must precede journal creation')
            self.claim(action);result=self.kernel('journal')
            return self.event('journal_ready' if result.get('ok') else 'journal_refused',result=result)
        if action=='pilot':
            if phase!='journal_ready':raise RuntimeError('Recovery journal must precede pilot')
            self.claim(action);self.event('pilot_running')
            result=self.kernel('apply',plans=[p for p in entries if p['manifest']==pilot])
            return self.event('pilot_applied' if result.get('ok') else 'pilot_deferred',result=result,pilot=pilot)
        if action=='remaining':
            if phase!='pilot_applied':raise RuntimeError('Applied pilot required')
            accepted=json.loads((self.root/'pilot.acceptance.private.json').read_text())
            if not accepted.get('passed') or accepted.get('pilot')!=pilot or accepted.get('binding_sha')!=digest(self.binding):
                raise RuntimeError('Independent pilot acceptance missing')
            self.claim(action);self.event('remaining_running')
            result=self.kernel('apply',plans=[p for p in entries if p['manifest']!=pilot])
            return self.event('completed' if result.get('ok') else 'partial_deferred',result=result,pilot=pilot)
        if action in ('rollback','watchdog'):
            if action=='watchdog' and (self.root/'pilot.acceptance.private.json').exists():
                accepted=json.loads((self.root/'pilot.acceptance.private.json').read_text())
                if accepted.get('passed') and accepted.get('pilot')==pilot and accepted.get('binding_sha')==digest(self.binding):
                    return {'phase':'watchdog_accepted_no_action'}
            state=self.run('status')
            if action=='watchdog' and state.get('phase') not in ('pilot_running','pilot_applied','pilot_deferred'):
                return {'phase':'watchdog_no_applied_pilot'}
            self.claim(action)
            journal=self.kernel('status')
            if not journal.get('ok'):return self.event('rollback_status_unavailable',result=journal)
            applied={r['manifest'] for r in journal['receipts'] if r['state']=='applied'}
            chosen=[p for p in reversed(entries) if p['manifest'] in applied and (action!='watchdog' or p['manifest']==pilot)]
            result=self.kernel('rollback',plans=chosen) if chosen else {'ok':True,'results':[]}
            return self.event('rolled_back' if result.get('ok') else 'rollback_refused_newer_data',result=result)
        raise RuntimeError('Unsupported host action')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True)
    parser.add_argument('--action',choices=('status','prepare','journal','pilot','remaining','rollback','watchdog'),default='status')
    parser.add_argument('--lab',action='store_true');args=parser.parse_args()
    try:
        result=Dispatcher(args.root,lab=args.lab).run(args.action)
        print(json.dumps({'phase':result.get('phase'),'summary':result.get('summary'),'ok':result.get('result',{}).get('ok')},separators=(',',':')))
    except Exception as exc:
        # Do not print a credential-bearing command, plan or traceback.
        print(json.dumps({'phase':'dispatcher_refused','error_type':type(exc).__name__}));raise SystemExit(1)


if __name__=='__main__':main()
