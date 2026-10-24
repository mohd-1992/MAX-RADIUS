"""Explicit NEW-domain bootstrap integration with frozen control code and test keys.

Runs in a disposable tools image. Uses a fresh Docker volume mounted at its own
daemon mountpoint so nested provisioning containers see the exact host paths.
No real domain/Nginx/tenant DB/router or production control storage is used.
"""
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from services.system_update_channel import verify_response


def main():
    if sys.argv[1:]!=['--run']:raise SystemExit('Explicit --run required')
    root=Path(os.environ['BOOTSTRAP_LAB_ROOT'])
    if not str(root).startswith('/var/lib/docker/volumes/maxnew_') or not root.is_dir():raise SystemExit('Fresh lab volume required')
    if (root/'ATTEMPTED').exists():raise SystemExit('Do not replay this lab')
    (root/'ATTEMPTED').write_text('Isolated new-domain creation bootstrap only')
    backend=Path('/fixture/control-source');sys.path.insert(0,str(backend))
    from platform_store import Store
    from release_center import ReleaseCenter
    spec=importlib.util.spec_from_file_location('new_domain_updates','/fixture/new_domain_updates.py')
    hook=importlib.util.module_from_spec(spec);spec.loader.exec_module(hook)
    name='fixture'+uuid.uuid4().hex[:8];tenant=root/'tenants'/name;tenant.mkdir(parents=True)
    store_root=root/'test-control';store_root.mkdir();vault=Fernet.generate_key();(store_root/'vault.key').write_bytes(vault)
    store=Store.__new__(Store);store.root=store_root;store.path=store_root/'platform.sqlite3';store.vault=Fernet(vault)
    with contextlib.closing(sqlite3.connect(store.path)) as db:
        db.execute('CREATE TABLE receipts(actor TEXT,key TEXT,fingerprint TEXT,result TEXT,PRIMARY KEY(actor,key))')
        db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,created TEXT DEFAULT CURRENT_TIMESTAMP,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,detail TEXT NOT NULL)')
    signing_key=Ed25519PrivateKey.generate();public=signing_key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    center=ReleaseCenter(store,types.SimpleNamespace(key=signing_key,public=public))
    cfg={'tenant_root':str(root/'tenants'),'state_root':str(root/'controllers'),'socket_root':str(root/'sockets'),
         'base_domain':'example.test','control_backend':str(backend),'control_store':str(store_root),
         'control_source_sha256':{n:hashlib.sha256((backend/n).read_bytes()).hexdigest() for n in ('platform_store.py','release_center.py','local_releases.py','password_policy.py')},
         'publisher_key_sha256':'060168f73961b3c4c7e47144285f92bb902b78802a6ae64b4fe15f2d9c2fca84',
         'key_fingerprint':hook.PUBLIC_TRUST}
    settings=root/'settings.json';settings.write_text(json.dumps(cfg));hook.SETTINGS=settings
    reference='mohd777/max-radius-web@sha256:7a9dacdc402a1a01c7481740a1f41a0ecfef8be4fa92948d1ecb1de9a0a1490b'
    image=json.loads(hook.run(['docker','image','inspect',reference]))[0]
    (tenant/'tenant.json').write_text(json.dumps({'subdomain':name,'instance_uuid':'INST-'+uuid.uuid4().hex[:10].upper(),'tunnel_mode':'central'}))
    (tenant/'creation-release.json').write_text(json.dumps({'web':{'reference':reference,'image_id':image['Id']}}))
    document={'services':{'web':{'image':reference,'environment':{},'volumes':['./storage:/app/storage']},
                          'radius_core':{'image':'mohd777/max-radius-core@sha256:b0e1b2e1f4a9fd35845800013fe59b95536efaaa71548e1a34ed6e1556da3a00'},
                          'db':{'image':'mariadb:10.11'}},'networks':{'tenant_net':{'name':name+'_tenant_net'}}}
    (tenant/'docker-compose.yml').write_text(json.dumps(document))
    # Same official registration transaction is idempotent before any generator runs.
    one=hook.register_new_target(tenant,name);two=hook.register_new_target(tenant,name)
    assert one==two and one['identity']==name+'.example.test'
    hook.prepare_new_domain_updates(tenant,name)
    installed=json.loads((root/'controllers'/name/'update-channel.private.json').read_text())
    assert installed['reader_id']==one['reader_id'] and installed['token']==one['token']
    assert one['token'] not in (tenant/'docker-compose.yml').read_text()
    assert one['token'] not in (root/'controllers'/(name+'.bootstrap.private.json')).read_text()
    import yaml
    generated=yaml.safe_load((tenant/'docker-compose.yml').read_text())
    assert generated['services']['db']==document['services']['db'] and generated['services']['radius_core']==document['services']['radius_core']
    assert generated['services']['update_controller']['container_name']=='max_radius_'+name+'_updates'
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM update_targets').fetchone()[0]==1
        row=db.execute('SELECT secret FROM update_targets').fetchone()
        assert row['secret']!=one['token'] and store.decrypt(row['secret'])==one['token']
        assert db.execute("SELECT COUNT(*) FROM audit WHERE actor='old-panel:new-domain'").fetchone()[0]==1
    assert (store_root/'vault.key').read_bytes()==vault
    # Exercise real signed control output against the compiled 2.6.4 consumer.
    draft=center.draft({'version':'2.6.5','title':'Fixture 2.6.5','changelog':['fixture only'],
             'images':{'web':reference},'scope':'domain','target':one['reader_id'],
             'min_client_version':'2.6.4','migration':'none','rollback':'fixture compatible'},'fixture-publisher','fixture-draft-0001')
    center.publish(draft['id'],{'revision':1,'confirm_scope':one['identity'],'artifacts_verified':True},'fixture-publisher','fixture-publish-0001')
    response=center.feed(one['token'],'2.6.4',['web','core','signed_audience'])
    trust=dict(installed,public_key=base64.b64encode(public).decode(),key_fingerprint=hashlib.sha256(public).hexdigest())
    assert verify_response(response,trust,'2.6.4')['version']=='2.6.5'
    other=center.target({'kind':'domain','identity':'other.example.test','label':'other fixture'},'fixture','fixture-target-other')
    assert center.feed(other['token'],'2.6.4',['web','core','signed_audience'])['has_update'] is False
    foreign=dict(trust,reader_id=other['id'],identity='other.example.test')
    try:verify_response(response,foreign,'2.6.4')
    except Exception:pass
    else:raise AssertionError('A different installation accepted the signed offer')
    result={'passed':True,'official_target_registration':True,'registration_idempotent':True,'credential_encrypted_in_control':True,
            'compiled_provisioner_real_nested_docker':True,'real_compose_config_valid':True,'existing_core_db_definitions_preserved':True,
            'compiled_consumer_accepts_real_targeted_manifest':True,'foreign_target_rejected':True,
            'production_store_or_domain_used':False,'nas_or_license_acceptance':False,'tenant_services_started':False}
    (root/'RESULTS.private.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()
