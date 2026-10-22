"""Create a fresh owned Docker lab; no remote access, app import, or publication."""
import ast
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = Path(r'C:/Users/lenovo/Documents/Codex/private/max-radius/as-client-access-check-20261016/monitor-reports')
CORE = 'sha256:4d609e0c03a32be6a979dfd110ccd10d8c8ceda72c3495a1c4734de653f42d59'
DB = 'sha256:0d0945c57f75b3a3b9ebe71aedaaad005fa5305054e8fca3d2e42a8c92b43b5e'


def main():
    assert sys.argv[1:] == ['--run'], 'Explicit --run required'
    owner = 'max-acctid-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dt%H%M%S') + '-' + uuid.uuid4().hex[:6]
    report = REPORT_ROOT / owner
    report.mkdir()
    (REPORT_ROOT / 'active-accounting-identity-lab.private.txt').write_text(str(report))
    stage = report / 'fixture'
    stage.mkdir()
    owned = []
    serial = 0

    def command(args, check=True, timeout=90):
        nonlocal serial
        serial += 1
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        (report / f'command-{serial:03d}.stdout.private').write_bytes(p.stdout)
        (report / f'command-{serial:03d}.stderr.private').write_bytes(p.stderr)
        if check and p.returncode:
            raise RuntimeError(f'Lab command {serial} failed with code {p.returncode}')
        return p.stdout.decode(errors='replace').strip(), p.returncode

    def docker(*args, **kw):
        return command(['docker', *args], **kw)

    def wait_ready(name):
        started = docker('inspect', name, '--format', '{{.State.StartedAt}}')[0]
        for _ in range(40):
            logs = docker('logs', '--since', started, name)[0]
            if 'Ready to process requests' in logs:
                return
            if docker('inspect', name, '--format', '{{.State.Running}}')[0] != 'true':
                raise RuntimeError('Owned FreeRADIUS failed startup')
            time.sleep(0.5)
        raise RuntimeError('Owned FreeRADIUS readiness timed out')

    def states():
        ids = docker('ps', '-aq')[0].splitlines()
        out = {}
        for offset in range(0, len(ids), 100):
            data = json.loads(docker('inspect', *ids[offset:offset + 100])[0])
            # Full inspect (which can contain environments) stays in private
            # report files.  The public evidence contains only bounded state.
            for c in data:
                out[c['Id']] = {'started': c['State']['StartedAt'],
                                'running': c['State']['Running'], 'restarts': c['RestartCount']}
        return out

    before = states()
    result = {'owner': owner, 'customer_access': False, 'published_ports': False,
              'remote_or_registry_push': False, 'existing_services_changed': False}
    try:
        runner_image = docker('image', 'inspect', 'dc3ba09b43b6', '--format', '{{.Id}}')[0]
        for image in [CORE, DB, runner_image]:
            assert docker('image', 'inspect', image, '--format', '{{.Id}}')[0] == image
        # Extract table DDL, never seed rows or a customer dump.
        source = (ROOT / 'database/schema_mysql.sql').read_text(encoding='utf-8')
        tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+`?\w+`?\s*\(.*?\) ENGINE=.*?;', source, re.S)
        assert len(tables) >= 40
        maintenance = ast.parse((ROOT / 'database/maintenance_jobs.py').read_text(encoding='utf-8'))
        tables.append(next(ast.literal_eval(n.value) for n in maintenance.body
                           if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
                           and t.id == 'MAINTENANCE_JOBS_DDL' for t in n.targets)))
        values = {}
        wanted = {'ACTIVE_SESSION_PREDICATE', 'PENDING_RESERVATION_PREDICATE',
                  'SCHEMA_SQL', 'CHECK_SQL', 'RESERVE_SQL', 'LINK_SQL', 'STOP_SQL'}
        tree = ast.parse((ROOT / 'database/license_capacity.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted:
                values[node.targets[0].id] = eval(compile(ast.Expression(node.value), '<fixture-sql>', 'eval'), {'__builtins__': {}}, values)
        tree = ast.parse((ROOT / 'database/cycle_safety.py').read_text(encoding='utf-8'))
        values['GUARD_TABLE_SQL'] = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'GUARD_TABLE_SQL' for t in n.targets))
        (stage / 'schema.json').write_text(json.dumps({'tables': tables, **values}), encoding='utf-8')
        shutil.copy2(ROOT / 'tests/accounting_identity_live.py', stage / 'driver.py')
        for name in ['entrypoint.sh', 'queries.conf', 'license-capacity-postauth.conf', 'max-accounting-identity.conf']:
            shutil.copy2(ROOT / 'docker/freeradius' / name, stage / name)
        entrypoint = (stage / 'entrypoint.sh').read_text(encoding='utf-8')
        # Verbose diagnostics ONLY inside the owned fixture image.
        entrypoint = entrypoint.replace('exec freeradius -f -l stdout', 'exec freeradius -X')
        (stage / 'entrypoint.sh').write_text(entrypoint, encoding='utf-8', newline='\n')
        begin = entrypoint.index('# Preserve maxcap accounting identity')
        end = entrypoint.index('# Reserve only after successful authentication', begin)
        (stage / 'baseline-entrypoint.sh').write_text(entrypoint[:begin] + entrypoint[end:], encoding='utf-8', newline='\n')
        # A pinned local base, no apt/pip, no registry lookup/push.
        base_tag = owner + ':base'
        candidate_tag = owner + ':candidate'
        docker('tag', CORE, base_tag)
        (stage / 'Dockerfile').write_text(
            f'FROM {base_tag}\nCOPY entrypoint.sh /entrypoint.sh\n'
            'COPY queries.conf /etc/freeradius/3.0/mods-config/sql/main/mysql/queries.conf\n'
            'COPY license-capacity-postauth.conf /etc/freeradius/3.0/license-capacity-postauth.conf\n'
            'COPY max-accounting-identity.conf /etc/freeradius/3.0/policy.d/max-accounting-identity\n'
            'RUN chmod +x /entrypoint.sh\n', encoding='utf-8')
        docker('build', '--network', 'none', '--pull=false', '-t', candidate_tag, str(stage), timeout=180)
        result['candidate_image'] = docker('image', 'inspect', candidate_tag, '--format', '{{.Id}}')[0]
        result['source_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir() if p.is_file()}
        nets = docker('network', 'ls', '-q')[0].splitlines()
        allocated = []
        for net in json.loads(docker('network', 'inspect', *nets)[0]):
            for config in net.get('IPAM', {}).get('Config') or []:
                if config.get('Subnet'):
                    allocated.append(ipaddress.ip_network(config['Subnet']))
        # Benchmark-only space, explicitly isolated.  Do not prune existing
        # networks when Docker's automatic address pools are exhausted.
        subnet = next(ipaddress.ip_network(f'198.18.{i}.0/24') for i in range(201, 251)
                      if not any(ipaddress.ip_network(f'198.18.{i}.0/24').overlaps(n)
                                 for n in allocated if n.version == 4))
        docker('network', 'create', '--internal', '--subnet', str(subnet),
               '--label', 'max.acctid.owner=' + owner, owner)
        db_name = owner + '-db'
        docker('create', '--name', db_name, '--network', owner, '--label', 'max.acctid.owner=' + owner,
               '--memory', '768m', '-e', 'MARIADB_ROOT_PASSWORD=fixture-acctid-db-only',
               '-e', 'MARIADB_ROOT_HOST=%', DB)
        owned.append(db_name)
        docker('start', db_name)
        for _ in range(50):
            if docker('exec', db_name, 'mariadb', '--protocol=tcp', '-h127.0.0.1',
                      '-uroot', '-pfixture-acctid-db-only', '-N', '-e', 'SELECT 1', check=False)[1] == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Disposable database startup timed out')
        for schema in ['identity_baseline', 'identity_candidate']:
            docker('exec', db_name, 'mariadb', '-uroot', '-pfixture-acctid-db-only', '-e',
                   'CREATE DATABASE ' + schema + ' CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        runner = owner + '-runner'
        docker('create', '--name', runner, '--network', owner, '--label', 'max.acctid.owner=' + owner,
               '--memory', '256m', '-e', 'MAX_ACCTID_FIXTURE=' + owner, '-e', 'DB_HOST=' + db_name,
               '-e', 'DB_NAME=identity_candidate', '--entrypoint', 'sleep', runner_image, 'infinity')
        owned.append(runner)
        docker('cp', str(stage), runner + ':/fixture')
        docker('start', runner)
        for variant in ['baseline', 'candidate']:
            schema = 'identity_' + variant
            docker('exec', '-e', 'DB_NAME=' + schema, runner, 'python', '/fixture/driver.py', '--setup')
            core = owner + '-' + variant
            image = CORE if variant == 'baseline' else result['candidate_image']
            docker('create', '--name', core, '--network', owner, '--label', 'max.acctid.owner=' + owner,
                   '--memory', '256m', '-e', 'DB_HOST=' + db_name, '-e', 'DB_NAME=' + schema,
                   '-e', 'DB_USER=root', '-e', 'DB_PASSWORD=fixture-acctid-db-only',
                   '-e', 'RADIUS_SECRET_DEFAULT=fixture-acctid-only', '--entrypoint', '/bin/bash', image, '/entrypoint.sh')
            owned.append(core)
            if variant == 'baseline':
                docker('cp', str(stage / 'baseline-entrypoint.sh'), core + ':/entrypoint.sh')
                docker('cp', str(stage / 'queries.conf'), core + ':/etc/freeradius/3.0/mods-config/sql/main/mysql/queries.conf')
                docker('cp', str(stage / 'license-capacity-postauth.conf'), core + ':/etc/freeradius/3.0/license-capacity-postauth.conf')
            docker('start', core)
            wait_ready(core)
            text, rc = docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + core,
                              runner, 'python', '/fixture/driver.py', '--baseline' if variant == 'baseline' else '--test',
                              check=False, timeout=180)
            docker('logs', core)
            result[variant + '_exit_code'] = rc
            if variant == 'candidate':
                docker('cp', runner + ':/fixture/result.json', str(report / 'test-results.private.json'), check=False)
            if rc:
                raise RuntimeError(variant + ' test run failed')
            if variant == 'candidate':
                docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + core,
                       runner, 'python', '/fixture/driver.py', '--stress', timeout=180)
                docker('cp', runner + ':/fixture/stress-result.json', str(report / 'stress-results.private.json'))
                docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + core,
                       runner, 'python', '/fixture/driver.py', '--open-before-restart')
                docker('restart', '-t', '5', core)
                wait_ready(core)
                docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + core,
                       runner, 'python', '/fixture/driver.py', '--resume-after-restart')
                result['restart_continuation_and_stop_passed'] = True
                # A rollback must retain the lookup bridge for sessions that
                # already received the new stable ID.  Do not restore a DB or
                # switch blindly to an image that only knows the stock hash.
                policy = (stage / 'max-accounting-identity.conf').read_text(encoding='utf-8')
                begin = policy.index("            elsif (&request:Tmp-String-9 == 'new') {")
                end = policy.index("            elsif (&request:Tmp-String-9 == 'ambiguous')", begin)
                compat = policy[:begin] + "            elsif (&request:Tmp-String-9 == 'new') {\n                noop\n            }\n" + policy[end:]
                (stage / 'compat-policy.conf').write_text(compat, encoding='utf-8')
                (stage / 'Dockerfile.compat').write_text(
                    f'FROM {candidate_tag}\nCOPY compat-policy.conf /etc/freeradius/3.0/policy.d/max-accounting-identity\n', encoding='utf-8')
                compat_tag = owner + ':compat-rollback'
                docker('build', '--network', 'none', '--pull=false', '-f', str(stage / 'Dockerfile.compat'),
                       '-t', compat_tag, str(stage), timeout=180)
                result['compatible_rollback_image'] = docker('image', 'inspect', compat_tag, '--format', '{{.Id}}')[0]
                result['compatible_rollback_policy_sha256'] = hashlib.sha256(compat.encode()).hexdigest()
                docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + core,
                       runner, 'python', '/fixture/driver.py', '--open-before-restart')
                docker('stop', '-t', '5', core)
                compat_core = owner + '-compat-rollback'
                docker('create', '--name', compat_core, '--network', owner, '--label', 'max.acctid.owner=' + owner,
                       '--memory', '256m', '-e', 'DB_HOST=' + db_name, '-e', 'DB_NAME=' + schema,
                       '-e', 'DB_USER=root', '-e', 'DB_PASSWORD=fixture-acctid-db-only',
                       '-e', 'RADIUS_SECRET_DEFAULT=fixture-acctid-only', '--entrypoint', '/bin/bash', compat_tag, '/entrypoint.sh')
                owned.append(compat_core)
                docker('start', compat_core)
                wait_ready(compat_core)
                docker('exec', '-e', 'DB_NAME=' + schema, '-e', 'RADIUS_HOST=' + compat_core,
                       runner, 'python', '/fixture/driver.py', '--resume-after-restart')
                result['compatible_rollback_continuation_and_stop_passed'] = True
        result['successful'] = True
    except BaseException:
        result['successful'] = False
        (report / 'exception.traceback.private').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        for name in reversed(owned):
            data = json.loads(docker('inspect', name)[0])[0]
            assert data['Config']['Labels'].get('max.acctid.owner') == owner
            docker('logs', name, check=False)
            docker('stop', '-t', '5', name, check=False)
        after = states()
        result['existing_services_changed'] = any(after.get(key) != value for key, value in before.items())
        result['new_owned_containers_retained_stopped'] = owned
        result['network_internal'] = True
        (report / 'FINAL.private.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'lab_saved': str(report), 'successful': result['successful'],
                          'existing_services_changed': result['existing_services_changed']}))
    return 0 if result['successful'] and not result['existing_services_changed'] else 1


if __name__ == '__main__':
    sys.exit(main())
