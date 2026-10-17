"""Explicit local Docker fixture. No client access, Docker socket or host system mounts."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

HERE=Path(__file__).resolve().parent
REPORT=Path(os.environ['CLOCK_GUARD_LINUX_REPORT_DIR']).resolve()
REPORT.mkdir(parents=True,exist_ok=False)
container=None


def command(args,timeout=45):
    p=subprocess.run(args,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Owned Linux fixture command failed')
    return p.stdout.decode().strip()


try:
    image=command(['docker','image','inspect','--format','{{.Id}}','max-radius-clock-guard-lab:20261017'])
    container=command(['docker','create','--name','max-clock-boot-files-'+uuid.uuid4().hex[:10],
        '--label','max-owned-task=clock-boot-prevention-local','--network','none','--restart=no',
        '-e','MAX_CLOCK_GUARD_FIXTURE=clock-boot-stability-20261017','--entrypoint','sleep',image,'infinity'])
    command(['docker','cp',str(HERE)+'/.',container+':/fixture'])
    command(['docker','start',container])
    command(['docker','exec',container,'sh','-c','mkdir -p /run/systemd/timesync; cp /fixture/fixture_probe.py /usr/bin/timedatectl; chmod 755 /usr/bin/timedatectl'])
    results=[]
    for file in ('test_clock_ready.py','test_managed_files.py','test_power_files.py','linux_checks.py'):
        p=subprocess.run(['docker','exec',container,'/usr/bin/python3','/fixture/'+file],capture_output=True,timeout=60)
        (REPORT/(file+'.stdout.private')).write_bytes(p.stdout);(REPORT/(file+'.stderr.private')).write_bytes(p.stderr)
        if p.returncode:raise RuntimeError('Linux fixture check failed: '+file)
        match=re.search(rb'Ran (\d+) tests',p.stderr)
        count=int(match[1]) if match else json.loads(p.stdout)['passed']
        results.append({'check':file,'count':count,'passed':True})
    runtime=command(['docker','inspect','--format','{{json .HostConfig.Privileged}} {{json .HostConfig.NetworkMode}}',container])
    final={'success':True,'checks':results,'total':sum(x['count'] for x in results),'container':container,'image':image,
        'runtime':runtime,'systemd_pid1':False,'physical_power_cut':False,'customer_access':False,
        'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in HERE.glob('*.py')}}
    (REPORT/'FINAL.private.json').write_text(json.dumps(final,indent=2));print(json.dumps({'success':True,'passed':final['total'],'network':'none','privileged':False,'real_boot_tested':False}))
except Exception as exc:
    (REPORT/'failure.private.json').write_text(json.dumps({'exception':type(exc).__name__,'reason':str(exc)}))
    print(json.dumps({'success':False,'private_diagnostics':True,'exception':type(exc).__name__}))
finally:
    if container:
        p=subprocess.run(['docker','stop',container],capture_output=True,timeout=35)
        (REPORT/'retained-container.private.json').write_text(json.dumps({'id':container,'retained':True,'stop_exit':p.returncode}))
