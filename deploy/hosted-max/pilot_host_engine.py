"""Two-service scoped switch. Never pull latest, touch DB containers or restore SQL.

This executable requires an operator-owned private plan and an existing claimed
job. It is not enabled by importing it, preparing a plan or publishing a release.
"""
import copy
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.parse

ROLES = ('web', 'core')
SERVICES = {'web': 'web', 'core': 'radius_core'}


def write(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as out:
        os.chmod(tmp, 0o600)
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


class Engine:
    def __init__(self, root):
        self.root = Path(root)
        self.plan = json.loads((self.root/'plan.private.json').read_text())
        if self.plan.get('domain') != '4k.max-net.net' or self.plan.get('project') != '4k':
            raise ValueError('Only the 4k pilot is permitted')
        if set(self.plan.get('old', {})) != set(ROLES) or set(self.plan.get('images', {})) != set(ROLES):
            raise ValueError('Only Web/Core may be switched')
        self.job = self.plan['job']
        if not re.fullmatch('[0-9a-f]{32}', self.job):
            raise ValueError('Invalid job identity')
        self.state = json.loads((self.root/'state.json').read_text())

    def command(self, args, timeout=120, input=None, check=True):
        p = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
        stamp = str(time.time_ns())
        (self.root/(stamp+'.stdout.private')).write_bytes(p.stdout)
        (self.root/(stamp+'.stderr.private')).write_bytes(p.stderr)
        if check and p.returncode:
            raise RuntimeError('Scoped operation failed; inspect the private job receipt')
        return p.stdout, p.returncode

    def docker(self, *args, **kw):
        return self.command(['docker', *args], **kw)[0].decode().strip()

    def inspect(self, identity):
        return json.loads(self.docker('inspect', identity))[0]

    def api(self, method, path, value=None):
        c = http.client.HTTPConnection('localhost', timeout=120)
        c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.sock.settimeout(120)
        c.sock.connect('/var/run/docker.sock')
        try:
            c.request(method, '/v1.44'+path, json.dumps(value).encode() if value is not None else None,
                      {'Content-Type': 'application/json'})
            response = c.getresponse()
            raw = response.read()
            if response.status not in (200, 201, 204):
                raise RuntimeError('Scoped Docker operation rejected')
            return json.loads(raw) if raw else None
        finally:
            c.close()

    def progress(self, status, percent, stage, **extra):
        self.state.update(status=status, percent=percent, stage=stage, **extra)
        write(self.root/'state.json', self.state)
        public={k:v for k,v in self.state.items() if k in ('status','percent','stage','completed_at')}
        public.update(release=self.plan['release'],domain='4k.max-net.net')
        target=self.root.parent.parent/'public-progress.json';write(target,public);os.chmod(target,0o644)

    def validate(self):
        if self.docker('info', '--format', '{{.ID}}') != self.plan['docker_server_id']:
            raise RuntimeError('Docker host binding changed')
        for role in ROLES:
            old = self.plan['old'][role]
            actual = self.inspect(old['Id'])
            labels = actual['Config'].get('Labels') or {}
            if (actual['Id'] != old['Id'] or actual['Image'] != old['Image']
                or actual['Name'] != old['Name'] or not actual['State']['Running']
                or actual['State'].get('Health',{}).get('Status') != 'healthy'
                or actual['State']['StartedAt'] != old['State']['StartedAt']
                or actual['RestartCount'] != old['RestartCount']
                or actual['Mounts'] != old['Mounts']
                or actual['NetworkSettings']['Networks'] != old['NetworkSettings']['Networks']
                or labels.get('com.docker.compose.project') != '4k'
                or labels.get('com.docker.compose.service') != SERVICES[role]):
                raise RuntimeError('Scoped live identity or binding changed')
        database=self.inspect(self.plan['database']['Id'])
        if (database['Id']!=self.plan['database']['Id'] or database['Image']!=self.plan['database']['Image']
            or database['Mounts']!=self.plan['database']['Mounts'] or not database['State']['Running']
            or database['Config'].get('Labels',{}).get('com.docker.compose.project')!='4k'
            or database['Config'].get('Labels',{}).get('com.docker.compose.service')!='db'):
            raise RuntimeError('Database scope changed')
        inventory=self.api('GET','/containers/json?all=1')
        ids=[v['Id'] for v in inventory if v['State']=='running']
        owned={v['Id'] for v in self.plan['old'].values()}
        all_items=json.loads(self.docker('inspect',*ids)) if ids else []
        tenant_networks=set(self.plan['old']['web']['NetworkSettings']['Networks'])
        if any(v['State']['Running'] and v['Config'].get('Labels',{}).get('com.docker.compose.project')!='4k'
               and tenant_networks.intersection(v['NetworkSettings']['Networks']) for v in all_items):
            raise RuntimeError('A foreign live service shares the tenant network')
        self.state['unrelated']={v['Id']:self.identity(v) for v in all_items if v['Id'] not in owned}
        self.state['unrelated_inventory']={v['Id']:self.inventory_identity(v) for v in inventory if v['Id'] not in owned}
        write(self.root/'state.json',self.state)
        for name,digest in self.plan['program_sha256'].items():
            if not re.fullmatch(r'[A-Za-z0-9_]+\.py',name) or hashlib.sha256((self.root.parent.parent/name).read_bytes()).hexdigest()!=digest:
                raise RuntimeError('Approved operation program changed')
        if self.plan.get('entitlement_policy') != 'verified-4k-demo-subscribers-original-migration':
            raise RuntimeError('Entitlement migration was not approved')
        for image in list(self.plan['images'].values())+[self.plan['compatible_core']]:
            if not re.fullmatch('sha256:[0-9a-f]{64}', image):
                raise RuntimeError('Unpinned image')
            if self.docker('image', 'inspect', image, '--format', '{{.Id}}') != image:
                raise RuntimeError('Verified image cache unavailable')

    def endpoints(self, old):
        result = {}
        for name, v in old['NetworkSettings']['Networks'].items():
            if not v.get('IPAddress'):
                raise RuntimeError('Static scoped bridge identity unavailable')
            aliases = [a for a in v.get('Aliases') or [] if a not in (old['Id'], old['Id'][:12], old['Config'].get('Hostname'))]
            result[name] = {'IPAMConfig': {'IPv4Address': v['IPAddress']}, 'Aliases': aliases}
        return result

    @staticmethod
    def identity(value):
        return {'image':value['Image'],'started':value['State']['StartedAt'],'running':value['State']['Running'],
                'restarts':value['RestartCount'],'mounts':value['Mounts']}

    @staticmethod
    def inventory_identity(value):
        return {'image':value['ImageID'],'state':value['State'],'mounts':value['Mounts']}

    def verify_unrelated(self):
        frozen_inventory=self.state.get('unrelated_inventory',{})
        if frozen_inventory:
            current={v['Id']:self.inventory_identity(v) for v in self.api('GET','/containers/json?all=1')}
            if any(current.get(k)!=v for k,v in frozen_inventory.items()):
                raise RuntimeError('An unrelated container inventory changed')
        expected_items=self.state.get('unrelated',{})
        # One frozen batch avoids a subprocess per historical local fixture.
        # Missing identities remain a failure; no moving ps window is used.
        actual_items={v['Id']:v for v in json.loads(self.docker('inspect',*expected_items))} if expected_items else {}
        for ident,expected in expected_items.items():
            if ident not in actual_items or self.identity(actual_items[ident])!=expected:
                raise RuntimeError('An unrelated service changed; do not control it from this update')

    def spec(self, role, image):
        old = self.plan['old'][role]
        keep = {'User','AttachStdin','AttachStdout','AttachStderr','ExposedPorts','Tty','OpenStdin','StdinOnce','Env','Cmd','Healthcheck','ArgsEscaped','Volumes','WorkingDir','Entrypoint','NetworkDisabled','OnBuild','Labels','StopSignal','StopTimeout','Shell'}
        result = {k: copy.deepcopy(v) for k, v in old['Config'].items() if k in keep}
        result['Env'] = [v for v in result.get('Env', []) if not v.startswith('HOSTNAME=')]
        result['Image'] = image
        result.setdefault('Labels', {})['max.pilot.job'] = self.job
        result['HostConfig'] = copy.deepcopy(old['HostConfig'])
        result['HostConfig'].pop('Binds', None)
        if any(v['Type'] not in ('bind', 'volume', 'tmpfs') for v in old['Mounts']):
            raise RuntimeError('Unsupported storage binding')
        result['HostConfig']['Mounts'] = [dict(Type=v['Type'], Source=v.get('Name') if v['Type']=='volume' else v['Source'], Target=v['Destination'], ReadOnly=not v['RW']) for v in old['Mounts'] if v['Type'] in ('bind', 'volume')]
        result['HostConfig']['RestartPolicy'] = {'Name':'no','MaximumRetryCount':0}
        result['NetworkingConfig'] = {'EndpointsConfig':self.endpoints(old)}
        return result

    def detach(self, identity):
        for name in self.inspect(identity)['NetworkSettings']['Networks']:
            self.docker('network','disconnect',name,identity)

    def attach(self, identity, old):
        current = self.inspect(identity)['NetworkSettings']['Networks']
        for name, value in self.endpoints(old).items():
            if name in current:
                continue
            args = ['network','connect','--ip',value['IPAMConfig']['IPv4Address']]
            for alias in value['Aliases']:
                args.extend(['--alias',alias])
            self.docker(*(args+[name,identity]))

    def wait(self, identity, role):
        end = time.monotonic()+120
        while time.monotonic()<end:
            actual = self.inspect(identity)
            if not actual['State']['Running']:
                raise RuntimeError('Scoped service stopped')
            if actual['State'].get('Health',{}).get('Status') not in (None,'healthy'):
                time.sleep(1)
                continue
            if role=='web':
                raw,rc=self.command(['docker','exec',identity,'curl','-sf','--max-time','3','-o','/dev/null','http://127.0.0.1:5090/login'],check=False)
                if rc==0:return
            elif ':0715 ' in self.docker('exec',identity,'cat','/proc/net/udp'):
                return
            time.sleep(1)
        raise RuntimeError('Scoped service health timeout')

    def probe(self, web, mode):
        # Frozen operator-reviewed code: no request body can supply a program.
        path=self.root.parent.parent/'probe.py'
        expected=self.plan['program_sha256']['probe.py']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Runtime verification program changed')
        checkpoint=self.root/'accounting-checkpoint.private.json'
        payload={'checkpoint':json.loads(checkpoint.read_text()) if checkpoint.exists() else []}
        result={}
        for attempt in range(12):
            result=json.loads(self.docker('exec','-i',web,'python','-c',path.read_text(),mode,input=json.dumps(payload).encode()))
            if result.get('passed') is True:break
            time.sleep(5)
        if result.get('passed') is not True:
            raise RuntimeError('Runtime signature, license or lease verification failed')
        if mode in ('candidate','final') and result.get('publisher_key')!=self.plan['publisher_key_sha256']:
            raise RuntimeError('Candidate publisher trust changed')
        if mode in ('before','rollback'):
            for name,digest in self.plan['legacy_web_source_sha256'].items():
                if name not in ('/app/services/system_update_service.py','/app/web/templates/tools/system_update.html'):
                    raise RuntimeError('Unexpected legacy source check')
                actual=self.docker('exec',web,'sha256sum',name).split()[0]
                if actual!=digest:raise RuntimeError('Retained legacy Web source changed')
        write(self.root/('probe-'+mode+'.private.json'),result)

    def prepare_backup(self):
        # An explicit helper builds/verifies/encrypts the DB/storage/config set;
        # its frozen path and receipt are required, never an engine/Telegram send.
        program=self.root.parent.parent/'backup.py'
        if hashlib.sha256(program.read_bytes()).hexdigest()!=self.plan['program_sha256']['backup.py']:
            raise RuntimeError('Backup program changed')
        self.command([sys.executable,str(program),str(self.root)],timeout=300)
        receipt=json.loads((self.root/'backup-verified.private.json').read_text())
        if not all(receipt.get(k) is True for k in ('verified','database','storage','configuration','encrypted','private_key_offhost')):
            raise RuntimeError('Mandatory complete backup did not verify')

    def preserve(self):
        self.state.setdefault('services',{})
        for role in ROLES:
            old=self.plan['old'][role];saved=self.state['services'].setdefault(role,{})
            self.docker('stop','-t','20',old['Id'])
            saved['stopped']=True;write(self.root/'state.json',self.state)
        for role in ROLES:
            old=self.plan['old'][role];saved=self.state['services'][role]
            saved['preserved_image']=self.docker('commit',old['Id'],'max-4k-preserved-'+self.job+':'+role)
            write(self.root/'state.json',self.state)
            self.docker('update','--restart=no',old['Id'])
            self.docker('rename',old['Id'],old['Name'].lstrip('/')+'_previous_'+self.job)
            saved['renamed']=True;write(self.root/'state.json',self.state);self.detach(old['Id'])

    def migrate(self):
        helper=self.root.parent.parent/'migrate.py'
        if hashlib.sha256(helper.read_bytes()).hexdigest()!=self.plan['program_sha256']['migrate.py']:
            raise RuntimeError('Migration program changed')
        self.command([sys.executable,str(helper),str(self.root)],timeout=240)
        if json.loads((self.root/'migration-verified.private.json').read_text()).get('verified') is not True:
            raise RuntimeError('Migration invariants failed')

    def create(self, role, image):
        name=self.plan['old'][role]['Name'].lstrip('/')
        # Discover an ambiguous create response by fixed name plus immutable
        # image and job label. Never retry a create before resolving its effect.
        try:
            value=self.api('POST','/containers/create?name='+urllib.parse.quote(name),self.spec(role,image))
            identity=value['Id']
        except Exception:
            actual=self.inspect(name)
            if actual['Image']!=image or actual['Config'].get('Labels',{}).get('max.pilot.job')!=self.job:
                raise
            identity=actual['Id']
        self.state['services'][role]['new_id']=identity;write(self.root/'state.json',self.state)
        self.docker('start',identity);self.wait(identity,role)
        return identity

    def rollback(self):
        if (self.root/'ACCEPTED').exists() or self.state.get('status') in ('rolled_back','recovery_required'):
            return
        self.progress('running',75,'استعادة الخدمات السابقة مع أحدث المحاسبة')
        self.state.setdefault('services',{})
        for role in ROLES:
            saved=self.state['services'].get(role,{})
            name=self.plan['old'][role]['Name'].lstrip('/')
            if not saved.get('new_id'):
                raw,rc=self.command(['docker','inspect',name],check=False)
                if rc==0:
                    item=json.loads(raw)[0]
                    if item['Id']!=self.plan['old'][role]['Id'] and item['Config'].get('Labels',{}).get('max.pilot.job')==self.job:
                        saved['new_id']=item['Id'];self.state['services'][role]=saved
            if saved.get('new_id'):
                item=self.inspect(saved['new_id'])
                if item['Image']!=self.plan['images'][role] or item['Config'].get('Labels',{}).get('max.pilot.job')!=self.job:
                    raise RuntimeError('Candidate rollback identity changed')
                if item['State']['Running']:self.docker('stop','-t','20',item['Id'])
                self.docker('update','--restart=no',item['Id']);self.detach(item['Id'])
                self.docker('rename',item['Id'],name+'_failed_'+self.job)
        # Core's lookup bridge is necessary for sessions written by the new
        # identity policy. It is separately tested and must be cached/preserved.
        for role in ('core','web'):
            old=self.plan['old'][role];saved=self.state['services'].get(role,{})
            actual=self.inspect(old['Id']);name=old['Name'].lstrip('/')
            if role=='core' and (saved.get('renamed') or actual['Name'].lstrip('/')!=name):
                identity=saved.get('rollback_id')
                if not identity:
                    try:
                        value=self.api('POST','/containers/create?name='+urllib.parse.quote(name),self.spec(role,self.plan['compatible_core']))
                        identity=value['Id']
                    except Exception:
                        found=self.inspect(name)
                        if found['Image']!=self.plan['compatible_core'] or found['Config'].get('Labels',{}).get('max.pilot.job')!=self.job:
                            raise
                        identity=found['Id']
                    saved['rollback_id']=identity;self.state['services'][role]=saved;write(self.root/'state.json',self.state)
                self.docker('start',identity)
            else:
                identity=old['Id']
                if actual['Name'].lstrip('/')!=name:self.docker('rename',identity,name)
                self.attach(identity,old)
                if not self.inspect(identity)['State']['Running']:self.docker('start',identity)
            self.wait(identity,role)
            self.docker('update','--restart='+old['HostConfig']['RestartPolicy']['Name'],identity)
        self.probe(self.plan['old']['web']['Id'],'rollback')
        self.restore_compose()
        self.verify_unrelated()
        self.progress('rolled_back',90,'فشل التحديث؛ تحققت عودة الخدمات السابقة دون استعادة قاعدة قديمة',error='لم يُثبت الإصدار الجديد؛ حُفظت أدلة الفشل والرجوع.')

    def restore_compose(self):
        if not (self.root/'compose-original.private').exists():return
        target=Path(self.plan['compose_path'])
        current=target.read_bytes()
        if hashlib.sha256(current).hexdigest() not in (self.plan['compose_sha256'],self.state.get('compose_candidate_sha256')):
            raise RuntimeError('Compose changed independently; recovery requires review')
        import yaml
        doc=yaml.safe_load((self.root/'compose-original.private').read_bytes())
        doc['services']['radius_core']['image']=self.plan['compatible_core']
        doc['services']['radius_core']['pull_policy']='never'
        tmp=target.with_suffix('.pilot-tmp');tmp.write_text(json.dumps(doc));os.chmod(tmp,0o600);os.replace(tmp,target)

    def persist_compose(self):
        import yaml
        target=Path(self.plan['compose_path']);original=target.read_bytes()
        if hashlib.sha256(original).hexdigest()!=self.plan['compose_sha256']:
            raise RuntimeError('Persistent Compose changed')
        doc=yaml.safe_load(original)
        if set(doc['services'])!={'web','radius_core','db'}:raise RuntimeError('Unexpected shared Compose component')
        (self.root/'compose-original.private').write_bytes(original)
        for role in ROLES:
            doc['services'][SERVICES[role]]['image']=self.plan['images'][role]
            doc['services'][SERVICES[role]]['pull_policy']='never'
        encoded=json.dumps(doc).encode();self.state['compose_candidate_sha256']=hashlib.sha256(encoded).hexdigest();write(self.root/'state.json',self.state)
        tmp=target.with_suffix('.pilot-tmp');tmp.write_bytes(encoded);os.chmod(tmp,0o600);os.replace(tmp,target)

    def apply(self):
        if (self.root/'ATTEMPTED').exists() or (self.root/'ACCEPTED').exists():
            raise RuntimeError('A claimed update cannot be replayed')
        self.validate();self.probe(self.plan['old']['web']['Id'],'before')
        self.progress('running',5,'التحقق من هوية 4k والنسخة الاحتياطية')
        self.prepare_backup()
        compose=Path(self.plan['compose_path']).read_bytes()
        if hashlib.sha256(compose).hexdigest()!=self.plan['compose_sha256']:
            raise RuntimeError('Compose changed before the switch')
        (self.root/'compose-original.private').write_bytes(compose)
        (self.root/'ATTEMPTED').write_text(dt.datetime.now(dt.timezone.utc).isoformat())
        try:
            self.progress('running',25,'حفظ Web وCore الحاليين وطبقاتهما')
            self.preserve()
            self.progress('running',45,'ترحيل مخطط 4k والتحقق من حفظ المحاسبة والمبالغ')
            self.migrate()
            self.progress('running',60,'تشغيل Web الخاصة بـ4k والتحقق من الترخيص')
            web=self.create('web',self.plan['images']['web']);self.probe(web,'candidate')
            self.progress('running',75,'تشغيل Core الخاصة بـ4k والتحقق من المجموعة')
            self.create('core',self.plan['images']['core'])
            self.probe(web,'final')
            self.verify_unrelated()
            self.persist_compose()
            for role in ROLES:
                self.docker('update','--restart='+self.plan['old'][role]['HostConfig']['RestartPolicy']['Name'],self.state['services'][role]['new_id'])
            (self.root/'ACCEPTED').write_text(dt.datetime.now(dt.timezone.utc).isoformat())
            self.progress('completed',100,'تحققت ترقية Web وCore لـ4k إلى 2.6.3',completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        except BaseException:
            if (self.root/'ACCEPTED').exists():
                # Acceptance is durable before the public projection. A failed
                # projection must not roll back an already accepted operation.
                self.progress('completed',100,'تحققت ترقية Web وCore لـ4k إلى 2.6.3',completed_at=(self.root/'ACCEPTED').read_text())
                return
            try:self.rollback()
            except BaseException:
                self.progress('recovery_required',80,'تعذر تأكيد الرجوع الكامل؛ يلزم فحص الإيصال قبل أي تكرار',error='تحتاج المهمة مراجعة؛ لا تعِد التثبيت.')
            raise


def main():
    action,root=sys.argv[1:]
    if action not in ('apply','rollback'):raise ValueError('Unknown action')
    engine=Engine(root)
    with (engine.root/'engine.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        engine.state=json.loads((engine.root/'state.json').read_text())
        try:
            if action=='apply':engine.apply()
            elif (engine.root/'ATTEMPTED').exists():engine.rollback()
        except BaseException:
            if not (engine.root/'ATTEMPTED').exists():
                engine.progress('error',5,'فشل التحقق قبل أي تبديل؛ الخدمات الحالية محفوظة',error='راجع الإيصال قبل إعادة الطلب.')
            raise


if __name__=='__main__':main()
