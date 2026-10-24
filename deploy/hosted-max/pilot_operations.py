"""Explicit 4k update helpers. No imports of the MAX application on the host."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile


def call(root,args,timeout=120,input=None):
    p=subprocess.run(args,capture_output=True,input=input,timeout=timeout)
    stamp=str(__import__('time').time_ns());(root/(stamp+'.stdout.private')).write_bytes(p.stdout);(root/(stamp+'.stderr.private')).write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('Private scoped helper failed')
    return p.stdout


def checked(root):
    plan=json.loads((root/'plan.private.json').read_text())
    if plan.get('domain')!='4k.max-net.net' or plan.get('project')!='4k':raise ValueError('Wrong helper scope')
    database=plan['database']
    actual=json.loads(call(root,['docker','inspect',database['Id']]))[0]
    if (actual['Id']!=database['Id'] or actual['Image']!=database['Image'] or actual['Mounts']!=database['Mounts']
        or actual['Config'].get('Labels',{}).get('com.docker.compose.project')!='4k'
        or actual['Config'].get('Labels',{}).get('com.docker.compose.service')!='db'):
        raise RuntimeError('The 4k database binding changed')
    return plan


def backup(root):
    from cryptography.hazmat.primitives import serialization,hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    plan=checked(root);database=plan['database'];environment=dict(v.split('=',1) for v in database['Config']['Env'] if '=' in v)
    web_env=dict(v.split('=',1) for v in plan['old']['web']['Config']['Env'] if '=' in v)
    password=environment.get('MARIADB_ROOT_PASSWORD') or environment.get('MYSQL_ROOT_PASSWORD')
    if not password:raise RuntimeError('Database backup credential unavailable')
    dump=call(root,['docker','exec','-e','MYSQL_PWD='+password,database['Id'],'mariadb-dump','-uroot','--single-transaction','--skip-lock-tables','--routines','--triggers','--hex-blob',web_env['DB_NAME']],timeout=180)
    if b'CREATE TABLE' not in dump or len(dump)<1000:raise RuntimeError('Incomplete database export')
    checkpoint_sql="SELECT JSON_OBJECT('radacctid',radacctid,'acctuniqueid',acctuniqueid,'acctinputoctets',acctinputoctets,'acctoutputoctets',acctoutputoctets,'acctsessiontime',acctsessiontime) FROM radacct ORDER BY radacctid DESC LIMIT 300"
    checkpoint=call(root,['docker','exec','-e','MYSQL_PWD='+password,database['Id'],'mariadb','-uroot','-N','-B',web_env['DB_NAME'],'-e',checkpoint_sql])
    (root/'accounting-checkpoint.private.json').write_text(json.dumps([json.loads(v) for v in checkpoint.decode().splitlines() if v.strip()]))
    storage=Path('/opt/clients/4k/storage');compose=Path(plan['compose_path'])
    if storage.resolve()!=storage or compose.parent.resolve()!=Path('/opt/clients/4k'):raise RuntimeError('Unexpected backup path')
    archive=root/'safety.tar.gz.private';payload_hashes={}
    def add_bytes(tar,name,data):
        info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;tar.addfile(info,io.BytesIO(data));payload_hashes[name]=hashlib.sha256(data).hexdigest()
    with tarfile.open(archive,'w:gz') as tar:
        add_bytes(tar,'database/full.sql',dump)
        add_bytes(tar,'configuration/docker-compose.yml',compose.read_bytes())
        add_bytes(tar,'configuration/plan.private.json',(root/'plan.private.json').read_bytes())
        for path in sorted(storage.rglob('*')):
            if path.is_symlink():raise RuntimeError('Storage symlink requires review')
            if path.is_file():
                name='storage/'+path.relative_to(storage).as_posix();data=path.read_bytes();add_bytes(tar,name,data)
        add_bytes(tar,'manifest.private.json',json.dumps(payload_hashes,sort_keys=True).encode())
    with tarfile.open(archive,'r:gz') as tar:
        for name,digest in payload_hashes.items():
            data=tar.extractfile(name).read()
            if hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('Safety archive verification failed')
    public=serialization.load_pem_public_key((root.parent.parent/'backup-public.pem').read_bytes())
    key=secrets.token_bytes(32);nonce=secrets.token_bytes(12);encryptor=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor();output=root/'safety.aesgcm.private'
    plain_hash=hashlib.sha256();cipher_hash=hashlib.sha256()
    with archive.open('rb') as source,output.open('wb') as target:
        os.chmod(output,0o600)
        while True:
            block=source.read(1048576)
            if not block:break
            plain_hash.update(block);encoded=encryptor.update(block);target.write(encoded);cipher_hash.update(encoded)
        encoded=encryptor.finalize();target.write(encoded);cipher_hash.update(encoded)
    receipt={'verified':True,'database':True,'storage':True,'configuration':True,'encrypted':True,'private_key_offhost':True,'payload_hashes':payload_hashes,'archive_sha256':plain_hash.hexdigest(),'ciphertext_sha256':cipher_hash.hexdigest(),'bytes':output.stat().st_size,'nonce':nonce.hex(),'tag':encryptor.tag.hex(),'wrapped_key':public.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None)).hex()}
    (root/'backup-verified.private.json').write_text(json.dumps(receipt,indent=2));archive.unlink()


MIGRATION=r'''
import json,os,re,hashlib
from database.db import query_all,query_one
from database.schema_healer import heal_database_schema
assert query_one('SELECT DATABASE() d')['d']=='radius_4k'
approved=set(json.loads(os.environ['MAX_4K_DEMO_SUBSCRIBERS']))
tables=[v['TABLE_NAME'] for v in query_all("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'")]
def snap():
 result={}
 for table in tables:
  assert re.fullmatch('[A-Za-z0-9_]+',table)
  result[table]=query_all('SELECT * FROM `'+table+'` ORDER BY 1')
 return result
before=snap()
assert heal_database_schema(backfill=False,install_triggers=True)
after=snap();changes=[]
for table,old in before.items():
 new=after[table];assert len(old)==len(new),'Unexpected row count change: '+table
 for a,b in zip(old,new):
  for col in a:
   if a[col]==b.get(col):continue
   ok=False
   if table=='wisp_voucher_sales' and col=='first_activation_voucher_id':ok=a[col] is None and b[col]==a['voucher_id']
   if table=='wisp_subscribers' and col=='snap_volume_quota_mb':ok=a['id'] in approved and a[col] is None and a['first_used_at'] is None and a['last_renewed_at'] is None
   if table=='wisp_vouchers' and col.startswith('snap_'):
    ok=(a['status'] in ('unused','suspended','disabled') and a['first_used_at'] is None and a['last_renewed_at'] is None and a['expires_at'] is None and b.get('policy_frozen_at') is None and b.get(col) is None)
   if not ok:raise RuntimeError('Unexpected existing business field change: '+table+'.'+col)
   changes.append(table+'.'+col)
assert before.get('radacct')==after.get('radacct')
checkpoint=query_all('SELECT radacctid,acctuniqueid,acctinputoctets,acctoutputoctets,acctsessiontime FROM radacct ORDER BY radacctid DESC LIMIT 300')
print('RESULT_JSON='+json.dumps({'verified':True,'tables':len(tables),'rows':sum(len(v) for v in before.values()),'allowed_changes':{v:changes.count(v) for v in set(changes)},'accounting_rows_unchanged':True,'checkpoint_private':checkpoint},default=str))
'''


def migrate(root):
    plan=checked(root);environment=dict(v.split('=',1) for v in plan['old']['web']['Config']['Env'] if '=' in v)
    environment.pop('HOSTNAME',None)
    for key,value in {'STORAGE_DIR':'/tmp/pilot-storage','BACKUPS_DIR':'/tmp/pilot-storage/backups','UPLOADS_DIR':'/tmp/pilot-storage/uploads','LOGS_DIR':'/tmp/pilot-storage/logs','MAX_4K_DEMO_SUBSCRIBERS':json.dumps(plan['demo_subscriber_ids'])}.items():environment[key]=value
    file=root/'migration.env.private';file.write_text('\n'.join(k+'='+v for k,v in environment.items())+'\n');os.chmod(file,0o600)
    networks=list(plan['old']['web']['NetworkSettings']['Networks'])
    if len(networks)!=1:raise RuntimeError('Ambiguous tenant network')
    name='max-4k-migration-'+plan['job']
    output=call(root,['docker','run','--name',name,'--network',networks[0],'--read-only','--tmpfs','/tmp','--env-file',str(file),'--label','max.pilot.job='+plan['job'],'--entrypoint','python',plan['images']['web'],'-c',MIGRATION],timeout=180)
    lines=[v for v in output.decode().splitlines() if v.startswith('RESULT_JSON=')]
    if len(lines)!=1:raise RuntimeError('Missing migration verification receipt')
    result=json.loads(lines[0].split('=',1)[1]);(root/'migration-verified.private.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    action,folder=sys.argv[1:];root=Path(folder)
    if action=='backup':backup(root)
    elif action=='migrate':migrate(root)
    else:raise ValueError('Unsupported helper')
