"""One-installation update socket. Run from a trusted, pinned 2.6.4 Web image.

Configuration and jobs live in a host-only mount, never the tenant's storage.
The only remote input accepted by apply is an immutable release selection.
"""
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sys
import time
import urllib.parse

from services.system_update_channel import configuration, fetch, public_result, verify_response
from services.system_update_executor import execute, owned_snapshot, prepare, run_job
from services.system_update_runtime import Docker, UpdateError, write_json

PUBLIC_FIELDS = ('status','percent','stage','message','error','target_version','started_at','completed_at','job')


class Controller:
    def __init__(self, root, api=None):
        self.root, self.api = Path(root), api or Docker()

    def config(self):
        value = configuration(self.root)
        # Explicit host paths are provisioned by the operator, never HTTP input.
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', value['project']):
            raise ValueError('Invalid project')
        if not value.get('compose_path') or not Path(value['compose_path']).is_absolute():
            raise ValueError('Persistent deployment definition is required')
        return value

    def current(self, config):
        old = owned_snapshot(self.api,config)
        return execute(self.api,old['web']['Id'],"import json;from core.config import APP_VERSION;print(json.dumps({'version':APP_VERSION}))")['version'], old

    def check(self):
        config = self.config(); current,_ = self.current(config)
        return public_result(fetch(config,current),config,current)

    def progress(self):
        try:
            pointer = json.loads((self.root/'current.private.json').read_text(encoding='utf-8'))
            if not re.fullmatch('[0-9a-f]{32}',pointer['job']): raise ValueError('Invalid job')
            state = json.loads((self.root/'jobs'/pointer['job']/'state.json').read_text(encoding='utf-8'))
            return {k:v for k,v in state.items() if k in PUBLIC_FIELDS}
        except FileNotFoundError:
            return {'status':'idle','percent':0,'stage':'بانتظار طلب مدير النظام'}

    def retry_review(self, receipt, config, current, old, response):
        """Recognize a finished pre-switch failure, including 2.6.4 journals.

        This is evaluated only on an explicit, freshly verified apply request.
        The prior worker must have exited normally; no service mutation or
        uncertain dispatch is eligible. The original journal is never erased.
        """
        try:
            original = receipt.read_bytes()
            claim = json.loads(original)
            job = claim['job']
            if not re.fullmatch('[0-9a-f]{32}', job): return None
            folder = self.root/'jobs'/job
            if folder.is_symlink() or receipt.is_symlink(): return None
            for name in ('state.json','request.private.json','helper.private.json','failure.private.json','PREPARATION_ATTEMPTED'):
                path = folder/name
                if path.is_symlink() or not path.is_file(): return None
            raw_state = (folder/'state.json').read_bytes()
            state = json.loads(raw_state)
            failure = json.loads((folder/'failure.private.json').read_text(encoding='utf-8'))
            if not isinstance(failure.get('type'), str) or not failure['type']: return None
            if (state.get('status') != 'error' or state.get('job') != job
                    or state.get('switch_attempted') or state.get('services')
                    or state.get('new_id') or state.get('create_attempted')): return None
            if state.get('stage') not in ('توقف التجهيز دون تبديل الخدمات','توقف التحديث قبل التبديل'): return None
            request = json.loads((folder/'request.private.json').read_text(encoding='utf-8'))
            prior = request['response']
            manifest = response['manifest']
            if (claim['release'] != manifest['release'] or request['current'] != current
                    or prior['manifest']['release'] != manifest['release']
                    or prior['offer']['manifest_sha256'] != response['offer']['manifest_sha256']
                    or state.get('target_version') != manifest['version']): return None
            helper = json.loads((folder/'helper.private.json').read_text(encoding='utf-8'))['id']
            if not re.fullmatch('[0-9a-f]{64}', helper): return None
            observed = self.api('GET','/containers/'+helper+'/json')
            labels = observed['Config'].get('Labels') or {}
            ended = observed['State']
            if (observed['Id'] != helper or labels.get('max.update.controller') != config['project']
                    or labels.get('max.update.job') != job or ended.get('Running')
                    or ended.get('Status') != 'exited' or ended.get('ExitCode') != 0): return None
            plan_path = folder/'plan.private.json'
            if plan_path.exists():
                if plan_path.is_symlink(): return None
                plan = json.loads(plan_path.read_text(encoding='utf-8'))
                if plan.get('project') != config['project'] or set(plan['old']) != set(old): return None
                for role, frozen in plan['old'].items():
                    actual = old[role]
                    if any(actual[k] != frozen[k] for k in ('Id','Image','Name','Config','HostConfig','Mounts')): return None
                    if actual['State']['StartedAt'] != frozen['State']['StartedAt']: return None
                compose = Path(plan['compose_path'])
                if compose.is_symlink() or hashlib.sha256(compose.read_bytes()).hexdigest() != plan['compose_sha256']: return None
            elif state.get('stage') != 'توقف التجهيز دون تبديل الخدمات':
                return None
            return {'previous_job':job,'previous_receipt':claim,
                    'receipt_sha256':hashlib.sha256(original).hexdigest(),
                    'state_sha256':hashlib.sha256(raw_state).hexdigest(),
                    'helper':helper,'reason':'confirmed_failure_before_service_switch'}
        except (OSError, ValueError, KeyError, TypeError, UpdateError):
            return None

    def submit(self, body):
        import fcntl
        if not isinstance(body,dict) or set(body) != {'version','backup_first','release_id','manifest_sha256'} or body['backup_first'] is not True:
            raise ValueError('Unsupported update request')
        with (self.root/'dispatch.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = self.progress()
            if state['status'] not in ('idle','completed','rolled_back','error'):
                return {'success':False,'message':'توجد مهمة محفوظة؛ راجع حالتها قبل إعادة الطلب.'}
            config = self.config(); current,old = self.current(config)
            response = fetch(config,current); manifest = verify_response(response,config,current)
            if (not manifest or manifest['version'] != body['version'] or manifest['release'] != body['release_id']
                    or response['offer']['manifest_sha256'] != body['manifest_sha256']):
                raise ValueError('Release changed or was withdrawn; review it again')
            # Explicit retries are allowed only after a confirmed pre-switch failure.
            receipt = self.root/('release-'+hashlib.sha256(manifest['release'].encode()).hexdigest()+'.private.json')
            review = None
            if receipt.exists():
                review = self.retry_review(receipt, config, current, old, response)
                if review is None:
                    return {'success':False,'message':'المحاولة السابقة لم يثبت توقفها قبل تبديل الخدمات؛ راجع حالتها قبل إعادة التحديث.'}
            import uuid
            job = uuid.uuid4().hex; folder = self.root/'jobs'/job; folder.mkdir(mode=0o700,parents=True)
            if review is not None:
                write_json(folder/'retry-review.private.json', dict(review, next_job=job, reviewed_at=time.time()))
            write_json(folder/'request.private.json',{'response':response,'current':current})
            write_json(folder/'state.json',{'status':'queued','percent':0,'stage':'التحقق والنسخ الإلزامي',
                        'job':job,'target_version':manifest['version'],'started_at':time.time()})
            write_json(receipt,{'job':job,'release':manifest['release']})
            write_json(self.root/'current.private.json',{'job':job})
            try:
                self.launch(config, old, folder, job)
            except Exception:
                write_json(folder/'state.json',{'status':'unknown','percent':0,'stage':'لم يتأكد بدء المهمة؛ لا تعِد الطلب','job':job})
                raise
            return {'success':True,'job':job,'target_version':manifest['version'],'message':'قُبلت مهمة التحديث؛ تابع الحالة المحفوظة.'}

    def launch(self, config, old, folder, job):
        # Current trusted image, fixed entrypoint, no environment from HTTP or tenant.
        helper = {'Image':old['web']['Image'],'Entrypoint':['python','-c'],
                  'Cmd':['from services.system_update_host import worker;worker("/max-update-private",'+repr(job)+')'],
                  'Env':['PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE=1'],
                  'Labels':{'max.update.controller':config['project'],'max.update.job':job},
                  'HostConfig':{'ReadonlyRootfs':True,'RestartPolicy':{'Name':'on-failure','MaximumRetryCount':1},
                      'Mounts':config['controller_mounts'], 'Tmpfs':{'/tmp':'rw,nosuid,size=33554432'},
                      'LogConfig':{'Type':'json-file','Config':{'max-size':'5m','max-file':'1'}}}}
        # A supervisor in the same helper survives loss of the worker process.
        created = self.api('POST','/containers/create?name=max_update_'+job,helper)
        write_json(folder/'helper.private.json',{'id':created['Id']})
        self.api('POST','/containers/'+created['Id']+'/start')

def worker(root, job):
    root=Path(root)
    if not re.fullmatch('[0-9a-f]{32}',job): raise ValueError('Invalid job')
    folder=root/'jobs'/job; state=json.loads((folder/'state.json').read_text())
    if state['status'] in ('completed','rolled_back','error','recovery_required'): return
    api=Docker(); controller=Controller(root,api); config=controller.config()
    # After a helper interruption, never prepare/pull/backup again over an existing plan.
    if (folder/'plan.private.json').exists():
        return run_job(folder,api)
    try:
        marker=folder/'PREPARATION_ATTEMPTED'
        if marker.exists():
            state.update(status='recovery_required',percent=0,stage='انقطع التجهيز؛ يلزم مراجعة الإيصال دون تكرار النسخ')
            write_json(folder/'state.json',state);return
        with marker.open('x') as out: out.write('Preparation is never replayed automatically')
        request=json.loads((folder/'request.private.json').read_text(encoding='utf-8'))
        response=fetch(config,request['current'])
        if response['offer']['manifest_sha256'] != request['response']['offer']['manifest_sha256']:
            raise ValueError('Release changed before preparation')
        prepare(api,config,response,request['current'],folder)
        # Backup/pull may take longer than an offer's five-minute lifetime.
        latest=fetch(config,request['current'])
        if not latest.get('has_update') or latest['offer']['manifest_sha256'] != response['offer']['manifest_sha256']:
            raise ValueError('Release was withdrawn or changed before switch')
        state.update(status='running',percent=45,stage='اكتمل النسخ والتحقق من الصور')
        write_json(folder/'state.json',state)
        return run_job(folder,api)
    except Exception as exc:
        # run_job journals its own recovery. Preparation has no service mutations.
        current=json.loads((folder/'state.json').read_text(encoding='utf-8'))
        if current.get('switch_attempted'): raise
        state.update(status='error',percent=0,stage='توقف التجهيز دون تبديل الخدمات',
                     message='لم تُبدّل أي خدمة. صحح سبب فشل التجهيز ثم أعد المحاولة من زر التحديث.')
        write_json(folder/'state.json',state)
        write_json(folder/'failure.private.json',{'type':type(exc).__name__,'phase':'preparation_before_switch'})


def main():
    if len(sys.argv)!=3: raise SystemExit('usage: update_controller.py PRIVATE_ROOT UNIX_SOCKET')
    root,path=Path(sys.argv[1]),Path(sys.argv[2]); controller=Controller(root);controller.config()
    serve(controller, path)


def serve(controller, path):
    import fcntl
    root, path = controller.root, Path(path)
    # A private, exclusively locked socket directory supports safe crash recovery.
    server_lock=(root/'server.lock').open('a')
    fcntl.flock(server_lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    if path.is_symlink(): raise SystemExit('Refusing a socket symlink')
    if path.exists():
        import stat
        if not stat.S_ISSOCK(path.stat().st_mode): raise SystemExit('Unexpected socket path')
        path.unlink()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def handle_request(self):
            try:
                if self.command=='GET' and self.path=='/_max_updates/v1/check': data=controller.check()
                elif self.command=='GET' and self.path=='/_max_updates/v1/progress': data=controller.progress()
                elif self.command=='POST' and self.path=='/_max_updates/v1/apply':
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=4096: raise ValueError('Invalid body size')
                    data=controller.submit(json.loads(self.rfile.read(size)))
                else:
                    self.send_error(403);return
                status=200
            except Exception:
                status=503;data={'success':False,'message':'تعذر التحقق من قناة التحديث أو المهمة؛ راجع الحالة المحفوظة.'}
            raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)
        do_GET=do_POST=do_PUT=do_DELETE=do_PATCH=do_HEAD=handle_request
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
    server=Server(str(path),Handler);os.chmod(path,0o660)
    try: server.serve_forever()
    finally: server.server_close();path.unlink(missing_ok=True)


if __name__=='__main__': main()
