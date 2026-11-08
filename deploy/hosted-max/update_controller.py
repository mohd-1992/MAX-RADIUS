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

PUBLIC_FIELDS = ('status','percent','stage','message','error','target_version','started_at','completed_at','job','engine_protocol')


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
        from services.system_update_sequence import gate
        return gate(public_result(fetch(config,current),config,current), self.progress())

    def progress(self):
        try:
            pointer = json.loads((self.root/'current.private.json').read_text(encoding='utf-8'))
            if not re.fullmatch('[0-9a-f]{32}',pointer['job']): raise ValueError('Invalid job')
            state = json.loads((self.root/'jobs'/pointer['job']/'state.json').read_text(encoding='utf-8'))
            return {k:v for k,v in state.items() if k in PUBLIC_FIELDS}
        except FileNotFoundError:
            return {'status':'idle','percent':0,'stage':'بانتظار طلب مدير النظام'}

    def engine(self):
        from services.system_update_requirements import ENGINE_PROTOCOL, CAPABILITIES
        return {'success':True,'engine_protocol':ENGINE_PROTOCOL,'project':self.config()['project'],
                'capabilities':list(CAPABILITIES)}

    def components(self):
        # Enrollment is operator-owned. A tenant socket cannot become a host
        # authority by submitting a manifest or a registry over HTTP.
        from services.system_component_control import ComponentController
        controller = ComponentController(self.root, self.api)
        registry = controller.config()
        config = self.config()
        if registry['authority'] != 'installation' or any(
                item['project'] != config['project'] for item in registry['components'].values()):
            raise ValueError('A host registry needs a separate host-only socket')
        return controller

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
            from services.system_update_safety import required
            if required(manifest['version']) and not config.get('executor_image'):
                raise ValueError('Prepared lease-safe executor is required before enrollment')
            # Explicit retries are allowed only after a confirmed pre-switch failure.
            receipt = self.root/('release-'+hashlib.sha256(manifest['release'].encode()).hexdigest()+'.private.json')
            review = None
            if state['status']=='error':
                # A new release must not bypass an unresolved prior worker either.
                try:
                    prior_job=state['job']
                    if not re.fullmatch('[0-9a-f]{32}',prior_job):raise ValueError('Invalid prior job')
                    prior_folder=self.root/'jobs'/prior_job
                    if prior_folder.is_symlink():raise ValueError('Invalid prior journal')
                    if (prior_folder/'request.private.json').is_symlink():raise ValueError('Invalid prior request')
                    prior_request=json.loads((prior_folder/'request.private.json').read_text())
                    prior_response=prior_request['response']
                    prior_receipt=self.root/('release-'+hashlib.sha256(prior_response['manifest']['release'].encode()).hexdigest()+'.private.json')
                    review=self.retry_review(prior_receipt,config,current,old,prior_response)
                except (OSError,ValueError,KeyError,TypeError):review=None
                if review is None:
                    return {'success':False,'message':'لم يثبت انتهاء المحاولة السابقة قبل التبديل؛ راجع الإيصال قبل تحديث آخر.'}
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
        helper_image = old['web']['Image']
        if config.get('executor_image'):
            reference = config['executor_image']
            if not re.fullmatch(r'mohd777/max-radius-web@sha256:[a-f0-9]{64}',reference):raise ValueError('Invalid prepared executor image')
            executor = self.api('GET','/images/'+urllib.parse.quote(reference,safe='')+'/json')
            if reference not in executor.get('RepoDigests',[]):raise ValueError('Prepared executor digest is missing')
            helper_image = executor['Id']
        helper = {'Image':helper_image,'Entrypoint':['python','-c'],
                  'Cmd':['from services.system_update_host import '+('supervise' if config.get('executor_image') else 'worker')+';'+('supervise' if config.get('executor_image') else 'worker')+'("/max-update-private",'+repr(job)+')'],
                  'Env':['PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE=1'],
                  'Labels':{'max.update.controller':config['project'],'max.update.job':job},
                  'HostConfig':{'ReadonlyRootfs':True,'RestartPolicy':{'Name':'on-failure','MaximumRetryCount':1},
                      'Memory':268435456,'NanoCpus':500000000,'PidsLimit':64,
                      'Mounts':config['controller_mounts'], 'Tmpfs':{'/tmp':'rw,nosuid,size=33554432'},
                      'LogConfig':{'Type':'json-file','Config':{'max-size':'5m','max-file':'1'}}}}
        # A supervisor in the same helper survives loss of the worker process.
        created = self.api('POST','/containers/create?name=max_update_'+job,helper)
        write_json(folder/'helper.private.json',{'id':created['Id']})
        self.api('POST','/containers/'+created['Id']+'/start')

def supervise(root, job):
    """Independent helper parent; only its own child can be terminated.

    A restarted helper recovers the journal instead of replaying preparation.
    The signed lease is never renewed or extended by this supervisor.
    """
    import subprocess
    if not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('Invalid job')
    folder=Path(root)/'jobs'/job
    code='from services.system_update_host import worker;worker('+repr(str(root))+','+repr(job)+')'
    child=subprocess.Popen([sys.executable,'-c',code])
    while child.poll() is None:
        try:
            state=json.loads((folder/'state.json').read_text())
            expired=state.get('forward_deadline') and time.monotonic()>=state['forward_deadline']
            if expired and state.get('switch_attempted') and not state.get('recovering') and state.get('status')=='running':
                child.terminate()
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
                return worker(root,job)
        except (OSError,ValueError):pass
        time.sleep(.5)
    state=json.loads((folder/'state.json').read_text())
    if child.returncode and state.get('status') not in ('completed','rolled_back','error','recovery_required'):
        return worker(root,job)


def worker(root, job):
    root=Path(root)
    if not re.fullmatch('[0-9a-f]{32}',job): raise ValueError('Invalid job')
    folder=root/'jobs'/job; state=json.loads((folder/'state.json').read_text())
    if state['status'] in ('completed','rolled_back','error','recovery_required'): return
    api=Docker(); controller=Controller(root,api); config=controller.config()
    def finish(result):
        if result.get('status')=='handoff_pending':
            from services.system_update_handoff import run
            return run(folder,api)
        return result
    if state['status']=='handoff_pending':return finish(state)
    # After a helper interruption, never prepare/pull/backup again over an existing plan.
    if (folder/'plan.private.json').exists():
        return finish(run_job(folder,api))
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
        return finish(run_job(folder,api))
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
    # Explicitly enrolled protocol4 controllers reconcile an interrupted worker
    # once under their own journal locks. Legacy installations are not enrolled
    # or upgraded by starting this server.
    if hasattr(controller, 'resume_interrupted'):
        controller.resume_interrupted()
    elif (root/'component-controller.private.json').exists():
        controller.components().resume_interrupted()
    if path.is_symlink(): raise SystemExit('Refusing a socket symlink')
    if path.exists():
        import stat
        if not stat.S_ISSOCK(path.stat().st_mode): raise SystemExit('Unexpected socket path')
        path.unlink()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def handle_request(self):
            try:
                if self.path.startswith('/_max_updates/v4/components/'):
                    from services.system_component_control import ComponentController
                    component = controller if isinstance(controller,ComponentController) else controller.components()
                    route = self.path.removeprefix('/_max_updates/v4/components/')
                    if self.command=='GET' and route=='engine': data=component.engine()
                    elif self.command=='GET' and route=='progress': data=component.progress()
                    elif self.command=='POST' and route in ('apply','recover'):
                        size=int(self.headers.get('Content-Length','0'))
                        if not 0<size<=262144: raise ValueError('Invalid component body size')
                        body=json.loads(self.rfile.read(size))
                        data=component.submit(body) if route=='apply' else component.recover(body)
                    else:
                        self.send_error(403);return
                elif self.command=='GET' and self.path=='/_max_updates/v1/check': data=controller.check()
                elif self.command=='GET' and self.path=='/_max_updates/v1/engine': data=controller.engine()
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
