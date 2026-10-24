"""Old-panel NEW-domain bootstrap only. No UI, fleet upgrade or release publishing.

Called inside the old panel's existing authenticated creation job/host lock.
The separate enrol subprocess uses only control's existing target-registration
service and transaction/audit receipt; it never starts a control server or worker.
"""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

SETTINGS = Path('/opt/saas-manager-state/new-domain-updates.json')
NAME = re.compile(r'[a-z0-9]{1,40}')
INSTANCE = re.compile(r'INST-[A-F0-9]{10}')
PIN = re.compile(r'mohd777/max-radius-web@sha256:[0-9a-f]{64}')
PUBLIC_TRUST = 'c780e786051edc07e9c309793c3bcd1fd3ba7881f645a7ccdd647f535bbdf5a4'


class BootstrapError(RuntimeError):
    pass


def atomic(path, data):
    path = Path(path)
    temporary = path.with_name('.'+path.name+'.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(args, timeout=120, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Enrolment stdout may contain a credential. Never forward subprocess text.
        raise BootstrapError('تعذر تجهيز قناة تحديث الدومين الجديد؛ راجع إيصال الإنشاء المحمي.')
    return result.stdout


def settings():
    value = json.loads(SETTINGS.read_text(encoding='utf-8'))
    required = {'tenant_root','state_root','socket_root','base_domain','control_backend','control_store',
                'control_source_sha256','publisher_key_sha256','key_fingerprint'}
    if set(value) != required or value['key_fingerprint'] != PUBLIC_TRUST:
        raise BootstrapError('إعداد مسار التحديث غير معتمد.')
    for key in ('tenant_root','state_root','socket_root','control_backend','control_store'):
        p = Path(value[key])
        if not p.is_absolute() or p.is_symlink():
            raise BootstrapError('مسار تهيئة التحديث غير صالح.')
    if not re.fullmatch(r'[a-z0-9.-]+', value['base_domain']):
        raise BootstrapError('نطاق الاستضافة غير صالح.')
    tenant = Path(value['tenant_root']).resolve()
    for key in ('state_root','socket_root'):
        private = Path(value[key]).resolve()
        if private == tenant or private.is_relative_to(tenant):
            raise BootstrapError('يجب عزل مخزن منفذ التحديث عن تخزين المنظومات.')
    return value


def preflight_new_domain_updates():
    """Read-only check, before the old creator reserves a tenant directory."""
    cfg = settings(); backend = Path(cfg['control_backend']); root = Path(cfg['control_store'])
    expected = cfg['control_source_sha256']
    if set(expected) != {'platform_store.py','release_center.py','local_releases.py','password_policy.py'}:
        raise BootstrapError('عقد تسجيل هويات التحديث غير مكتمل.')
    for name, digest in expected.items():
        path = backend/name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise BootstrapError('تغير عقد مركز التحديث؛ أوقف الإنشاء إلى حين مراجعة التوافق.')
    for name in ('platform.sqlite3','vault.key'):
        path = root/name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise BootstrapError('مخزن هوية التحديث غير متاح؛ لم ينشأ اعتماد بديل.')
    with contextlib.closing(sqlite3.connect((root/'platform.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'update_targets','receipts','audit'} <= tables:
            raise BootstrapError('سجل هوية التحديث غير جاهز.')
    return cfg


def context(directory, name, cfg):
    if not isinstance(name,str) or not NAME.fullmatch(name):
        raise BootstrapError('اسم منظومة غير صالح.')
    original = Path(directory)
    path = original.resolve()
    if original.is_symlink() or path != Path(cfg['tenant_root']).resolve()/name:
        raise BootstrapError('التهيئة مقصورة على مجلد الإنشاء الجديد.')
    for part in ('tenant.json','creation-release.json','docker-compose.yml'):
        if (path/part).is_symlink() or not (path/part).is_file():
            raise BootstrapError('ملفات الإنشاء المثبت غير مكتملة.')
    if any((path/n).exists() for n in ('_panel_create_failed.json','update-controller-provisioned.json')):
        raise BootstrapError('لا تعاد تهيئة منظومة قائمة أو محاولة إنشاء محفوظة.')
    meta = json.loads((path/'tenant.json').read_text(encoding='utf-8'))
    if meta.get('subdomain') != name or meta.get('tunnel_mode') != 'central' or not INSTANCE.fullmatch(str(meta.get('instance_uuid',''))):
        raise BootstrapError('هوية إنشاء المنظومة غير متطابقة.')
    release = json.loads((path/'creation-release.json').read_text(encoding='utf-8'))
    reference = release['web']['reference']
    if not isinstance(reference,str) or not PIN.fullmatch(reference):
        raise BootstrapError('يجب تثبيت صورة Web قبل تهيئة التحديث.')
    return path, meta, release


def register_new_target(directory, name):
    """Root-only local service delegation. No password, private signing key or HTTP login."""
    if os.name != 'posix' or os.geteuid() != 0:
        raise BootstrapError('تسجيل هويات الإنشاء مقصور على منفذ المضيف المعتمد.')
    cfg = preflight_new_domain_updates()
    path, meta, release = context(directory,name,cfg)
    # This process imports just existing storage/target services, never server/app.
    sys.path.insert(0,cfg['control_backend'])
    from platform_store import Store
    from release_center import ReleaseCenter
    root = Path(cfg['control_store'])
    store = Store.__new__(Store)
    store.root, store.path = root, root/'platform.sqlite3'
    store.vault = Store.validate_storage_key(root)
    if store.vault is None:
        raise BootstrapError('اعتماد تشفير مركز التحديث مفقود؛ لم يولد بديل.')
    center = ReleaseCenter.__new__(ReleaseCenter)
    center.store, center.signer = store, None
    identity = name+'.'+cfg['base_domain']
    key = 'new-domain-'+hashlib.sha256((name+'|'+meta['instance_uuid']).encode()).hexdigest()
    result = center.target({'kind':'domain','identity':identity,'label':identity},'old-panel:new-domain',key)
    return {'kind':'domain','identity':identity,'reader_id':result['id'],'token':result['token']}


def prepare_new_domain_updates(directory, name):
    cfg = preflight_new_domain_updates()
    path, meta, release = context(directory,name,cfg)
    existing = run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+name]).strip()
    if existing:
        raise BootstrapError('مسار الإنشاء الجديد لا يعدل منظومات أو حاويات قائمة.')
    private = Path(cfg['state_root'])/name
    sockets = Path(cfg['socket_root'])/name
    if private.exists() and any(private.iterdir()) or sockets.exists() and any(sockets.iterdir()):
        raise BootstrapError('توجد حالة تهيئة محفوظة؛ لا تعاد المحاولة تلقائيًا.')
    image = json.loads(run(['docker','image','inspect',release['web']['reference']]))[0]
    labels = image['Config'].get('Labels') or {}
    version = labels.get('org.opencontainers.image.version','')
    if (image['Id'] != release['web']['image_id'] or labels.get('max.release.channel') != 'control-v1'
            or not re.fullmatch(r'\d+\.\d+\.\d+',version) or tuple(map(int,version.split('.'))) < (2,6,4)):
        raise BootstrapError('صورة الإنشاء لا تدعم منفذ تحديث control المعتمد.')
    probe = ("import hashlib,json,subprocess;from services.system_update_provision import TRUST;"
             "p=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True);"
             "print(json.dumps({'verified':p.returncode==0,'key':hashlib.sha256(open('/opt/max-protection/runtime-public.pem','rb').read()).hexdigest(),'channel_key':TRUST['key_fingerprint']}))")
    result = json.loads(run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',image['Id'],'-c',probe],timeout=60))
    if result != {'verified':True,'key':cfg['publisher_key_sha256'],'channel_key':cfg['key_fingerprint']}:
        raise BootstrapError('لم يثبت توقيع صورة الإنشاء أو ثقة قناة التحديث.')
    for parent in (private.parent,sockets.parent):
        parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    journal = private.parent/(name+'.bootstrap.private.json')
    if journal.exists():
        raise BootstrapError('محاولة تهيئة الدومين محفوظة؛ راجع الإيصال دون إعادة التنفيذ.')
    atomic(journal,{'stage':'enrollment_claimed','instance':meta['instance_uuid'],'image':image['Id'],'started':time.time()})
    try:
        enrollment = json.loads(run([sys.executable,str(Path(__file__).resolve()),'enroll',str(path),name,str(SETTINGS)],timeout=40))
        if (set(enrollment) != {'kind','identity','reader_id','token'} or enrollment['kind'] != 'domain'
                or enrollment['identity'] != name+'.'+cfg['base_domain']):
            raise BootstrapError('لم تتطابق هوية تسجيل الدومين.')
        private.mkdir(mode=0o700,exist_ok=True);sockets.mkdir(mode=0o750,exist_ok=True)
        # Same absolute paths inside the provisioning container keep host mounts exact.
        code = ("import json,sys;from services.system_update_provision import provision;"
                "p=json.load(sys.stdin);r=provision(p['path'],p['name'],p['private'],p['socket'],enrollment=p['enrollment']);print(json.dumps(r))")
        body = json.dumps({'path':str(path),'name':name,'private':str(private),'socket':str(sockets),'enrollment':enrollment})
        args = ['docker','run','--rm','-i','--network','none','--read-only','--tmpfs','/tmp:rw,size=32m']
        for p in (path,private,sockets):args.extend(['--mount','type=bind,source='+str(p)+',target='+str(p)])
        args.extend(['--entrypoint','python',image['Id'],'-c',code])
        provisioned = json.loads(run(args,input=body,timeout=60))
        if provisioned.get('provisioned') is not True or provisioned.get('targeted_enrollment') is not True:
            raise BootstrapError('تعذر إكمال تهيئة منفذ التحديث.')
        import yaml
        document = yaml.safe_load((path/'docker-compose.yml').read_text(encoding='utf-8'))
        controller = document['services']['update_controller']
        controller['container_name'] = 'max_radius_'+name+'_updates'
        controller['network_mode'] = 'bridge'
        temporary = path/'.compose-updates.tmp'
        with temporary.open('w',encoding='utf-8') as stream:
            os.chmod(temporary,0o600);yaml.safe_dump(document,stream,sort_keys=False,allow_unicode=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path/'docker-compose.yml')
        run(['docker','compose','--project-directory',str(path),'-p',name,'config','--quiet'],timeout=30)
        atomic(journal,{'stage':'prepared','instance':meta['instance_uuid'],'image':image['Id'],
                        'reader_id':enrollment['reader_id'],'domain':enrollment['identity'],'version':version})
    except Exception as exc:
        atomic(journal,{'stage':'review_required','instance':meta['instance_uuid'],'error_type':type(exc).__name__})
        raise BootstrapError('توقفت تهيئة تحديث الدومين الجديد؛ حُفظ الإيصال ولم يبدأ تحديث أي منظومة قائمة.') from None


def verify_new_domain_updates(directory, name):
    cfg = settings(); path = Path(directory).resolve()
    if not NAME.fullmatch(name) or Path(directory).is_symlink() or path != Path(cfg['tenant_root']).resolve()/name:
        raise BootstrapError('نطاق تحقق تهيئة التحديث غير صالح.')
    private = Path(cfg['state_root'])/name
    config = json.loads((private/'update-channel.private.json').read_text(encoding='utf-8'))
    proof = json.loads((path/'update-controller-provisioned.json').read_text(encoding='utf-8'))
    if (config.get('project') != name or config.get('kind') != 'domain'
            or config.get('identity') != name+'.'+cfg['base_domain'] or proof.get('targeted_enrollment') is not True):
        raise BootstrapError('إعداد قناة تحديث الدومين غير مطابق.')
    image = json.loads(run(['docker','inspect','max_radius_'+name+'_web']))[0]
    version = json.loads(run(['docker','image','inspect',image['Image']]))[0]['Config']['Labels']['org.opencontainers.image.version']
    code = ("import json;from services.system_update_control import request;"
            "r=request('GET','check');p=request('GET','progress');"
            "assert r.get('success') is True and r.get('current_version')=="+repr(version)+" and p.get('status')=='idle';"
            "print(json.dumps({'verified':True,'version':r['current_version'],'install_started':False}))")
    result = None
    for attempt in range(5):
        try:
            result = json.loads(run(['docker','exec','max_radius_'+name+'_web','python','-c',code],timeout=25))
            break
        except BootstrapError:
            if attempt == 4:raise
            time.sleep(2)
    if not result or result.get('verified') is not True:
        raise BootstrapError('لم يتأكد اتصال قناة تحديث الدومين؛ لم يعلن نجاح الإنشاء.')
    atomic(private/'creation-ready.private.json',{'domain':config['identity'],'version':version,'verified':True,
                'controller_ready':True,'targeted_feed_verified':True,'install_started':False,'verified_at':time.time()})


if __name__ == '__main__':
    try:
        if len(sys.argv) not in (4,5) or sys.argv[1] != 'enroll': raise BootstrapError('Unsupported operation')
        if len(sys.argv) == 5:
            SETTINGS = Path(sys.argv[4])
            if not SETTINGS.is_absolute(): raise BootstrapError('Invalid settings path')
        print(json.dumps(register_new_target(sys.argv[2],sys.argv[3])))
    except Exception:
        print('New-domain enrolment failed; inspect protected creation receipts.',file=sys.stderr)
        raise SystemExit(1)
