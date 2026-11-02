"""Verify fixed snapshot handling with exact generated MariaDB objects.

Creates and removes one isolated, labelled, portless container. No live RADIUS.
Historical raw SQL restores still preserve unknown contracts; no guessed unlimited policy.
"""
import ast
import json
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, input=None):
    p = subprocess.run(['docker', *args], input=input, text=True, encoding='utf-8',
                       capture_output=True, timeout=40)
    if p.returncode:
        raise RuntimeError(p.stderr[:1200])
    return p.stdout.strip()


def objects():
    captured = []
    ns = {'ensure_schema_object': lambda cur, sql: captured.append(sql)}
    tree = ast.parse((ROOT / 'database/first_use_policy.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
             t.id in ('FIELDS', 'FREEZE_SQL') for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'first_use_policy.py', 'exec'), ns)

    class Cursor:
        def execute(self, sql): self.sql = sql
        def fetchone(self):
            if self.sql.startswith('SHOW COLUMNS'):
                return {'Null': 'YES', 'Default': None, 'Type': 'varchar(255)'}
            if self.sql.startswith('SHOW INDEX'): return {'found': 1}
            return None
        def close(self): pass
    class Connection:
        def cursor(self): return Cursor()
    ns['install_first_use_policy'](Connection())
    assert len(captured) == 5
    return captured


def main():
    name = 'rate-policy-audit-' + uuid.uuid4().hex[:10]
    label = 'max.audit.rate-policy=' + name
    image = docker('image', 'inspect', 'mariadb:10.11.8', '--format', '{{.Id}}')
    created = False
    try:
        docker('run', '-d', '--name', name, '--label', label, '--network', 'none',
               '--memory', '768m', '--tmpfs', '/var/lib/mysql:rw,size=384m',
               '-e', 'MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1', image)
        created = True
        for _ in range(40):
            try:
                docker('exec', name, 'mariadb-admin', '-uroot', 'ping')
                break
            except RuntimeError:
                time.sleep(1)
        else: raise RuntimeError('Local audit DB not ready')
        def sql(text):
            return docker('exec', '-i', name, 'mariadb', '-uroot', '-N', '-B', input=text)
        sql('''CREATE DATABASE audit CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE audit;
CREATE TABLE wisp_packages(id INT PRIMARY KEY,name VARCHAR(64),price DECIMAL(10,2),cost DECIMAL(10,2),
volume_quota_mb BIGINT,rate_download VARCHAR(50),rate_upload VARCHAR(50));
CREATE TABLE wisp_vouchers(id INT PRIMARY KEY,username VARCHAR(64),package_id INT,status VARCHAR(32),
first_used_at DATETIME,last_renewed_at DATETIME,expires_at DATETIME,policy_frozen_at DATETIME,
snap_price DECIMAL(10,2),snap_cost DECIMAL(10,2),snap_volume_quota_mb BIGINT,
snap_rate_download VARCHAR(50),snap_rate_upload VARCHAR(50),snap_rate_limit_str VARCHAR(255));
CREATE TABLE radgroupreply(id INT PRIMARY KEY,groupname VARCHAR(64),attribute VARCHAR(64),value VARCHAR(255));
CREATE TABLE wisp_subscribers(id INT PRIMARY KEY,package_id INT,snap_volume_quota_mb BIGINT);
INSERT INTO wisp_packages VALUES(1,'fixture',10,2,100,'4M','2M');''')
        for obj in objects():
            sql('USE audit;\nDELIMITER ;;\n' + obj + ';;\n')
        cases = []
        def expect(label, setup, expression, expected):
            actual = sql('USE audit;\n' + setup + '\n' + expression + ';')
            assert actual == expected, (label, actual, expected)
            cases.append(label)
        expect('new unused snapshot stays NULL',
               "INSERT INTO wisp_vouchers(id,username,package_id,status) VALUES(1,'fixture',1,'unused');",
               'SELECT snap_rate_limit_str IS NULL FROM wisp_vouchers WHERE id=1', '1')
        expect('first acceptance without group rate uses explicit unlimited',
               "DO fn_freeze_voucher_policy('fixture');",
               'SELECT snap_rate_limit_str FROM wisp_vouchers WHERE id=1', '0/0')
        expect('renewal blank survives update trigger and matches empty guard',
               "UPDATE wisp_vouchers SET status='active',snap_rate_limit_str='' WHERE id=1;",
               "SELECT COALESCE(snap_rate_limit_str,'0/0')='' FROM wisp_vouchers WHERE id=1", '1')
        expect('NULL rate uses unlimited fallback without matching blank guard',
               'UPDATE wisp_vouchers SET snap_rate_limit_str=NULL WHERE id=1;',
               "SELECT COALESCE(snap_rate_limit_str,'0/0') FROM wisp_vouchers WHERE id=1", '0/0')
        expect('restored active blank survives insert trigger',
               "SET @max_radius_import=1; INSERT INTO wisp_vouchers(id,username,package_id,status,snap_rate_limit_str) VALUES(2,'restore-fixture',1,'active','');",
               "SELECT snap_rate_limit_str='' FROM wisp_vouchers WHERE id=2", '1')
        expect('blank legacy group rate refuses first acceptance without freezing',
               "SET @max_radius_import=0; INSERT INTO radgroupreply VALUES(1,'fixture','MikroTik-Rate-Limit',''); INSERT INTO wisp_vouchers(id,username,package_id,status) VALUES(3,'legacy-fixture',1,'unused'); DO fn_freeze_voucher_policy('legacy-fixture');",
               "SELECT policy_frozen_at IS NULL FROM wisp_vouchers WHERE id=3", '1')
        print(json.dumps({'image': image, 'cases_passed': len(cases), 'cases': cases,
                          'scope': 'native MariaDB objects; no RADIUS/NAS authentication'}, indent=2))
    finally:
        if created:
            actual = docker('inspect', name, '--format', '{{index .Config.Labels "max.audit.rate-policy"}}')
            if actual != name: raise RuntimeError('Cleanup identity mismatch')
            docker('rm', '-f', name)


if __name__ == '__main__': main()
