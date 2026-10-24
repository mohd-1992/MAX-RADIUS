"""Opt-in wrapper for the 4k Unix fleet socket; other fleet units stay unchanged."""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid

ROOT=Path('/var/lib/max-4k-pilot')
PREFIX='/_max_pilot/4k/'


def atomic(path,value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as out:
        os.chmod(tmp,0o600);json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)


class Channel:
    def __init__(self,root=ROOT):self.root=Path(root)

    def configuration(self):
        config=json.loads((self.root/'configuration.private.json').read_text())
        if config.get('domain')!='4k.max-net.net' or config.get('project')!='4k':raise ValueError('Wrong scope')
        return config

    def release(self):
        config=self.configuration()
        if config.get('enabled') is not True:raise ValueError('Pilot not yet enabled')
        path=Path(config['catalogue_path'])
        release=json.loads(path.read_text())
        signed={k:v for k,v in release.items() if k!='signature'}
        from cryptography.hazmat.primitives import serialization
        import base64
        key=serialization.load_pem_public_key((self.root/'release-public.pem').read_bytes())
        key.verify(base64.b64decode(release['signature']),json.dumps(signed,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        if (release.get('domain')!='4k.max-net.net' or release.get('instance')!=config['instance']
            or release.get('version')!='2.6.3' or release.get('release')!=config['release']
            or release.get('images')!=config['images'] or release.get('components')!=['web','core']
            or release.get('plan_sha256')!=config['plan_sha256'] or release.get('automatic_install') is not False):
            raise ValueError('Unapproved release')
        return config,release

    def progress(self):
        try:
            pointer=json.loads((self.root/'current.private.json').read_text());job=pointer['job']
            if not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('Invalid pointer')
            state=json.loads((self.root/'jobs'/job/'state.json').read_text())
            return {k:v for k,v in state.items() if k in ('status','stage','percent','message','error','started_at','target_version','completed_at')}
        except FileNotFoundError:return {'status':'idle','percent':0,'stage':'بانتظار بدء التحديث من مدير 4k'}

    def check(self):
        config,release=self.release()
        atomic(self.root/'delivery-receipt.json',{'release':release['release'],'domain':'4k.max-net.net','instance':config['instance'],'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'available':True,'installed':False})
        os.chmod(self.root/'delivery-receipt.json',0o644)
        return {'success':True,'has_update':True,'latest_version':'2.6.3','target_domain':'4k.max-net.net','components':['web','core'],'title':'تحديث 2.6.3 — Web وCore لـ4k فقط','changelog':release['changelog'],'release_date':release['release_date'],'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'automatic_install':False}

    def submit(self,body):
        if body!={'version':'2.6.3','backup_first':True}:raise ValueError('Unsupported request')
        config,release=self.release()
        with (self.root/'claim.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if (self.root/'current.private.json').exists():
                # One approved trial, no implicit retry after errors or rollback.
                pointer=json.loads((self.root/'current.private.json').read_text())
                return {'success':True,'target_version':'2.6.3','message':'توجد مهمة محفوظة؛ راجع حالتها دون إعادة التنفيذ.','job':pointer['job']}
            source=self.root/'approved-plan.private.json';raw=source.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=config['plan_sha256']:raise ValueError('Plan changed')
            plan=json.loads(raw);job=uuid.uuid4().hex;folder=self.root/'jobs'/job;folder.mkdir(mode=0o700,parents=True)
            plan['job']=job
            atomic(folder/'plan.private.json',plan)
            atomic(folder/'state.json',{'status':'queued','stage':'تم قبول طلب مدير 4k؛ بانتظار التحقق والنسخ','percent':0,'target_version':'2.6.3','started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'services':{}})
            atomic(self.root/'current.private.json',{'job':job,'release':release['release']})
            # Intent precedes dispatch. If dispatch is uncertain the claimed job
            # remains unknown; another click never launches a second operation.
            timer='max-4k-pilot-'+job+'-guard'
            worker='max-4k-pilot-'+job
            try:
                subprocess.run(['systemd-run','--quiet','--unit='+timer,'--on-active=15min','/usr/bin/python3',str(self.root/'watchdog.py'),str(folder),worker],check=True,capture_output=True,timeout=15)
                subprocess.run(['systemd-run','--quiet','--unit='+worker,'--property=PrivateNetwork=yes','--property=RestrictAddressFamilies=AF_UNIX','--property=IPAddressDeny=any','/usr/bin/python3',str(self.root/'pilot_host_engine.py'),'apply',str(folder)],check=True,capture_output=True,timeout=15)
            except Exception:
                atomic(folder/'state.json',{'status':'unknown','stage':'لم يتأكد تسليم المهمة؛ لا تعِد الطلب قبل فحص الإيصال','percent':0,'target_version':'2.6.3','services':{}})
                raise
            return {'success':True,'target_version':'2.6.3','job':job,'message':'بدأت مهمة 4k المحفوظة على المضيف.'}

    def dispatch(self,tenant,method,path,body=None):
        if tenant!='4k' or not path.startswith(PREFIX):return 403,{'success':False}
        route=path[len(PREFIX):]
        if method=='GET' and route=='check':return 200,self.check()
        if method=='GET' and route=='progress':return 200,self.progress()
        if method=='POST' and route=='apply':return 200,self.submit(body)
        return 403,{'success':False}


def main():
    if sys.argv[1:]!=['4k']:raise SystemExit('4k socket only')
    config=Channel().configuration();original=Path(config['original_controller'])
    if hashlib.sha256(original.read_bytes()).hexdigest()!=config['original_controller_sha256']:raise SystemExit('Original controller changed')
    spec=importlib.util.spec_from_file_location('original_fleet',original);legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    channel=Channel()
    class Handler(legacy.Handler):
        def handle_request(self):
            if not self.path.startswith(PREFIX):return super().handle_request()
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>4096:raise ValueError('Body too large')
                body=json.loads(self.rfile.read(length)) if length else None
                status,data=channel.dispatch(self.server.tenant,self.command,self.path,body)
            except Exception:status,data=503,{'success':False,'message':'تعذر التحقق من قناة 4k؛ راجع الإيصال دون إعادة التنفيذ.'}
            raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        do_GET=do_POST=do_PUT=do_DELETE=do_PATCH=do_HEAD=handle_request
    if int(os.environ.get('LISTEN_PID','0'))!=os.getpid() or os.environ.get('LISTEN_FDS')!='1':raise SystemExit('One systemd socket required')
    server=legacy.Server('/run/max-panel-fleet/4k/docker.sock',Handler,bind_and_activate=False);server.socket.close();server.socket=socket.fromfd(3,socket.AF_UNIX,socket.SOCK_STREAM);server.tenant='4k';server.serve_forever()


if __name__=='__main__':main()
