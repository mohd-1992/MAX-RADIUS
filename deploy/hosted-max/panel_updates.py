"""Old panel adapter. Importing this module never enrolls or updates a tenant.

Existing installations enroll lazily, inside an explicit authenticated update job.
New installations keep their existing native controller and its release receipts.
"""
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time

import new_domain_updates as bootstrap

HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'update_support'))
PUBLIC_KEY = 'slJTMJ6HvelR8HIYKNMSAecLtRMU1OjWifrnhvf1Iyg='
TERMINAL = {'completed', 'error', 'rolled_back', 'recovery_required', 'unknown'}


class PanelUpdateError(RuntimeError):
    pass


def command(args, timeout=60, input=None):
    p = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise PanelUpdateError('تعذر تجهيز منفذ التحديث؛ راجع الإيصال المحمي دون إعادة المحاولة تلقائيًا.')
    return p.stdout


def context(name):
    cfg = bootstrap.preflight_new_domain_updates()
    if not bootstrap.NAME.fullmatch(name): raise PanelUpdateError('اسم المنظومة غير صالح.')
    path = Path(cfg['tenant_root'])/name
    if path.is_symlink() or path.resolve() != Path(cfg['tenant_root']).resolve()/name:
        raise PanelUpdateError('مسار المنظومة غير صالح.')
    meta = json.loads((path/'tenant.json').read_text(encoding='utf-8'))
    if meta.get('subdomain') != name or (path/'_panel_create_failed.json').exists():
        raise PanelUpdateError('هوية المنظومة غير مكتملة؛ أوقف التحديث.')
    identity_path = path/'storage'/'instance_id'
    identity = identity_path.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{6,128}', identity) or meta.get('instance_uuid',identity) != identity:
        raise PanelUpdateError('هوية التثبيت لا تطابق السجل؛ يلزم التحقق دون تغييرها.')
    return cfg, path, identity


def enrollment(name):
    """Only invoked by the explicit update job's root-only subprocess."""
    if os.name != 'posix' or os.geteuid() != 0: raise PanelUpdateError('يتطلب منفذ المضيف المعتمد.')
    cfg, path, instance = context(name)
    sys.path.insert(0,cfg['control_backend'])
    from platform_store import Store
    from release_center import ReleaseCenter
    root = Path(cfg['control_store'])
    store = Store.__new__(Store)
    store.root, store.path, store.vault = root, root/'platform.sqlite3', Store.validate_storage_key(root)
    if store.vault is None: raise PanelUpdateError('اعتماد مركز التحديث مفقود؛ لم يولد بديل.')
    # Reuse an existing active identity; never replace a token or register twice.
    with contextlib.closing(sqlite3.connect(store.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM update_targets WHERE kind=? AND identity=?',
                         ('domain',name+'.'+cfg['base_domain'])).fetchone()
    if row:
        if row['active'] != 1: raise PanelUpdateError('هوية التحديث معطلة في المركز.')
        result = {'id':row['id'],'token':store.decrypt(row['secret'])}
    else:
        center = ReleaseCenter.__new__(ReleaseCenter); center.store,center.signer=store,None
        key = 'old-panel-enroll-'+hashlib.sha256((name+'|'+instance).encode()).hexdigest()
        result = center.target({'kind':'domain','identity':name+'.'+cfg['base_domain'],
                                'label':name+'.'+cfg['base_domain']},'old-panel:update',key)
    return {'kind':'domain','identity':name+'.'+cfg['base_domain'],'reader_id':result['id'],'token':result['token']}


def request(socket_path, action, body=None):
    conn = http.client.HTTPConnection('localhost',timeout=25)
    conn.sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    conn.sock.settimeout(25)
    try:
        conn.sock.connect(str(socket_path))
        conn.request('GET' if body is None else 'POST','/_max_updates/v1/'+action,
                     body=json.dumps(body).encode() if body is not None else None,
                     headers={'Content-Type':'application/json'})
        res = conn.getresponse(); raw=res.read(262145)
        if res.status != 200 or len(raw)>262144: raise ValueError('Controller unavailable')
        result=json.loads(raw)
        if not isinstance(result,dict): raise ValueError('Invalid response')
        return result
    except Exception:
        raise PanelUpdateError('تعذر تأكيد حالة التحديث؛ لا تعِد الطلب قبل مراجعة الإيصال.') from None
    finally: conn.close()


def ensure_controller(name):
    """Called on click only. No Compose edits, Web restart, pull or backup here."""
    cfg,path,instance=context(name)
    root, sockets = Path(cfg['state_root'])/name, Path(cfg['socket_root'])/name
    config_path=root/'update-channel.private.json';marker=root/'panel-controller.private.json'
    if config_path.exists():
        from services.system_update_channel import configuration
        value=configuration(root)
        if (value['kind']!='domain' or value['project']!=name or value['identity']!=name+'.'+cfg['base_domain']
                or value['key_fingerprint']!=bootstrap.PUBLIC_TRUST):
            raise PanelUpdateError('إعداد منفذ التحديث لا يطابق المنظومة.')
        if marker.exists():
            proof=json.loads(marker.read_text())
            if proof.get('instance')!=instance or proof.get('status')!='ready':
                raise PanelUpdateError('تهيئة سابقة غير مؤكدة؛ راجع الإيصال دون تكرارها.')
            command(['systemctl','start','max-panel-update@'+name+'.service'])
        return root,sockets/'controller.sock'
    # Validate actual service ownership and trust before creating an identity.
    from services.system_update_executor import owned_snapshot, execute
    from services.system_update_runtime import Docker
    roles={'web':'web','core':'radius_core','db':'db'}
    old=owned_snapshot(Docker(),{'project':name,'services':roles})
    if any(m['Destination']=='/max-update-socket' for m in old['web']['Mounts']):
        raise PanelUpdateError('منفذ تحديث آخر موجود؛ يلزم التحقق قبل الربط.')
    trust=execute(Docker(),old['web']['Id'],
        "import json,hashlib,pathlib,subprocess;from core.config import APP_VERSION;"
        "p=subprocess.run(['python','/opt/max-protection/verify.py'],capture_output=True);"
        "print(json.dumps({'version':APP_VERSION,'verified':p.returncode==0,'key':hashlib.sha256(pathlib.Path('/opt/max-protection/runtime-public.pem').read_bytes()).hexdigest()}))")
    from services.system_update_channel import version
    version(trust['version'])
    if trust.get('verified') is not True or trust.get('key')!=cfg['publisher_key_sha256']:
        raise PanelUpdateError('لم يثبت توقيع النسخة الحالية ومصدرها المعتمد.')
    if root.exists() and any(root.iterdir()) or sockets.exists() and any(sockets.iterdir()):
        raise PanelUpdateError('توجد تهيئة غير مكتملة؛ لا تعاد تلقائيًا.')
    root.mkdir(mode=0o700,parents=True,exist_ok=True);sockets.mkdir(mode=0o750,parents=True,exist_ok=True)
    bootstrap.atomic(marker,{'status':'claimed','instance':instance})
    try:
        reader=json.loads(command([sys.executable,str(HERE/'panel_updates.py'),'enroll',name],timeout=40))
        if (set(reader)!={'kind','identity','reader_id','token'} or reader['kind']!='domain'
                or reader['identity']!=name+'.'+cfg['base_domain'] or not reader['reader_id'] or not reader['token']):
            raise ValueError('Invalid enrollment')
        value=dict(public_key=PUBLIC_KEY,key_fingerprint=bootstrap.PUBLIC_TRUST,**reader,
                   channel='https://control.max-net.net',executor='host',project=name,
                   services=roles,compose_path=str(path/'docker-compose.yml'),enrollment_socket=str(sockets))
        bootstrap.atomic(config_path,value)
        bootstrap.atomic(marker,{'status':'ready','instance':instance})
        command(['systemctl','enable','--now','max-panel-update@'+name+'.service'])
    except Exception:
        bootstrap.atomic(marker,{'status':'review_required','instance':instance})
        raise PanelUpdateError('تعذر إكمال الربط؛ حُفظ الإيصال دون تحديث المنظومة أو تكرار التسجيل.') from None
    return root,sockets/'controller.sock'


def wait_socket(path):
    for _ in range(15):
        if path.is_socket(): return
        time.sleep(0.2)
    raise PanelUpdateError('لم يجهز منفذ التحديث؛ لم يبدأ التثبيت.')


def update_tenant(name):
    root,sock=ensure_controller(name);wait_socket(sock)
    state=request(sock,'progress')
    if state.get('status') not in {'idle','completed','rolled_back','error'}:
        raise PanelUpdateError('توجد مهمة سابقة غير محسومة؛ راجع حالتها دون بدء تحديث جديد.')
    check=request(sock,'check')
    if check.get('success') is not True: raise PanelUpdateError('تعذر التحقق من قناة التحديث الجديدة.')
    if check.get('has_update') is False:
        return 'تم فحص مركز التحديث الجديد؛ لا يوجد إصدار أحدث منشور ومتوافق مع هذه المنظومة. لم يبدأ تحديث.'
    if check.get('has_update') is not True: raise PanelUpdateError('نتيجة إتاحة التحديث غير صالحة.')
    selected={k:check.get(k) for k in ('latest_version','release_id','manifest_sha256')}
    if not all(selected.values()): raise PanelUpdateError('بيان التحديث غير مكتمل.')
    reply=request(sock,'apply',{'version':selected['latest_version'],'release_id':selected['release_id'],
                               'manifest_sha256':selected['manifest_sha256'],'backup_first':True})
    if reply.get('success') is not True:
        raise PanelUpdateError('رفض منفذ التحديث البدء؛ راجع المحاولة المحفوظة ولا تعِدها تلقائيًا.')
    if not re.fullmatch('[0-9a-f]{32}',reply.get('job','')):
        raise PanelUpdateError('لم تتأكد هوية المهمة؛ راجع الإيصال دون إعادة الطلب.')
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        state=request(sock,'progress')
        if state.get('job')!=reply['job']: raise PanelUpdateError('تغيرت هوية المهمة؛ يلزم مراجعة الحالة.')
        if state.get('status')=='completed': return 'اكتمل التحديث من المركز الجديد وتحقق التشغيل مع حفظ المحاسبة.'
        if state.get('status') in TERMINAL:
            raise PanelUpdateError('لم يكتمل التحديث؛ الحالة المحفوظة: '+state['status']+'؛ لا تعِد المحاولة تلقائيًا.')
        time.sleep(2)
    raise PanelUpdateError('انتهى انتظار اللوحة؛ المهمة مستقلة عنها. راجع حالتها دون إعادة طلب التحديث.')


def guard_legacy_rollback(name):
    cfg,path,instance=context(name)
    if (Path(cfg['state_root'])/name/'update-channel.private.json').exists():
        raise PanelUpdateError('هذه المنظومة تستخدم مركز التحديث الجديد؛ الرجوع القديم غير متوافق. راجع إيصال المهمة.')


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='enroll': raise SystemExit(2)
    try: print(json.dumps(enrollment(sys.argv[2])))
    except Exception:
        print('Protected enrollment failed; review the receipt.',file=sys.stderr);raise SystemExit(1)


if __name__=='__main__':main()
