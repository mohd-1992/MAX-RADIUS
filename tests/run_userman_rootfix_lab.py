"""Create an owned local fixture from repository schema, retain it stopped.

Requires a NEW USERMAN_ROOTFIX_REPORT_DIR. Never copies tenant data or uses
tenant credentials. Run deliberately, not through unittest discovery.
"""
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

root=Path(__file__).resolve().parents[1]
reports=Path(os.environ['USERMAN_ROOTFIX_REPORT_DIR']).resolve()
reports.mkdir(mode=0o700,parents=True,exist_ok=False)
token=uuid.uuid4().hex[:12]
name='max-userman-rootfix-'+token+'-db'
schema='userman_rootfix_lab_'+token
password=secrets.token_hex(24)
container=None


def run(args, **kw):
    return subprocess.run(args,capture_output=True,**kw)


try:
    p=run(['docker','run','-d','--name',name,'--label','max-owned-task=userman-rootfix-local',
           '--restart=no','-p','127.0.0.1::3306','-e','MARIADB_ROOT_PASSWORD='+password,
           '-e','MARIADB_DATABASE='+schema,'mariadb:10.11'])
    (reports/'launch.stderr.private').write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('Isolated DB creation failed')
    container=p.stdout.decode().strip()
    ports=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .NetworkSettings.Ports}}',container]))
    port=ports['3306/tcp'][0]['HostPort']
    assert port!='3306'
    for _ in range(60):
        p=run(['docker','exec','-e','MYSQL_PWD='+password,container,'mariadb','-uroot','-N','-e','SELECT 1'])
        if p.returncode==0:break
        time.sleep(.5)
    else:raise RuntimeError('Isolated DB did not become ready')
    sql=("CREATE USER IF NOT EXISTS 'radius'@'%' IDENTIFIED BY 'fixture-only'; GRANT ALL ON "+schema+".* TO 'radius'@'%';\n").encode()+(root/'database/schema_mysql.sql').read_bytes()
    p=run(['docker','exec','-i','-e','MYSQL_PWD='+password,container,'mariadb','-uroot',schema],input=sql)
    (reports/'bootstrap.stdout.private').write_bytes(p.stdout);(reports/'bootstrap.stderr.private').write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('Fixture bootstrap failed')
    env=dict(os.environ,USERMAN_ROOTFIX_LAB='1',DB_TYPE='mysql',DB_HOST='127.0.0.1',DB_PORT=port,
             DB_NAME=schema,DB_USER='root',DB_PASSWORD=password,STORAGE_DIR=str(reports/'storage'))
    fd=os.open(reports/'fixture.private.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'container':container,'port':port,'schema':schema,'password':password},f)
    p=run([sys.executable,str(root/'tests/userman_rootfix_mariadb.py')],env=env,cwd=root)
    (reports/'suite.stdout.private').write_bytes(p.stdout);(reports/'suite.stderr.private').write_bytes(p.stderr)
    if p.returncode:raise RuntimeError('Fixture checks failed; see private diagnostics')
    result=json.loads(p.stdout)
    (reports/'RESULTS.private.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
finally:
    if container:
        # An exact newly-created container ID, never an existing service name.
        p=run(['docker','stop',container],timeout=45)
        (reports/'stop.stderr.private').write_bytes(p.stderr)
