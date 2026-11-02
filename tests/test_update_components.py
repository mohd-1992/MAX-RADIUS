"""Isolated signed component coordination; no application imports or real NAS."""
import base64
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services import system_update_components as engine


class FakeDocker:
    def __init__(self):
        self.rows, self.calls, self.images = {}, [], {}
        self.lost_create = False
    def add(self, key, project, role, image):
        ident = hashlib.sha256(key.encode()).hexdigest()
        self.rows[ident] = {'Id': ident, 'Image': image, 'Name': '/'+key,
            'State': {'Running': True, 'StartedAt': 'baseline', 'Health': {'Status': 'healthy'}},
            'Config': {'Image': image, 'Env': ['FIXTURE=preserved'], 'Cmd': ['mariadbd'] if role == 'db' else [],
                'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': role, 'max.db.scope': 'shared' if key == 'shared-db' else 'dedicated'}},
            'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}, 'NetworkMode': 'none'},
            'Mounts': [{'Type': 'volume', 'Name': key+'-data', 'Source': '/fixture/'+key, 'Destination': '/var/lib/mysql', 'RW': True}] if role == 'db' else [{'Type': 'volume', 'Name': key+'-storage', 'Source': '/fixture/'+key+'-storage', 'Destination': '/app/storage', 'RW': True}] if role == 'web' else [],
            'NetworkSettings': {'Networks': {'none': {}}}}
        return ident
    def __call__(self, method, path, body=None, timeout=30):
        self.calls.append((method, path))
        route = urllib.parse.urlsplit(path)
        bits = route.path.strip('/').split('/')
        query = urllib.parse.parse_qs(route.query)
        if path == '/containers/json?all=1':
            return [{'Id': k, 'Labels': v['Config']['Labels'], 'State': 'running' if v['State']['Running'] else 'exited', 'Names': [v['Name']]} for k, v in self.rows.items()]
        if bits[:2] == ['containers', 'create']:
            name = '/'+query['name'][0]
            if any(v['Name'] == name for v in self.rows.values()):
                raise engine.UpdateError('Name occupied')
            ident = hashlib.sha256((name+str(len(self.rows))).encode()).hexdigest()
            self.rows[ident] = {'Id': ident, 'Image': body['Image'], 'Name': name,
                'Config': {k: copy.deepcopy(v) for k, v in body.items() if k not in ('HostConfig', 'NetworkingConfig')},
                'HostConfig': copy.deepcopy(body['HostConfig']), 'Mounts': [], 'State': {'Running': False, 'StartedAt': 'new', 'Health': {'Status': 'healthy'}}, 'NetworkSettings': {'Networks': {'none': {}}}}
            mounts = body['HostConfig'].get('Mounts', [])
            self.rows[ident]['Mounts'] = [{'Type': m['Type'], 'Name': m['Source'], 'Source': '/fixture/'+m['Source'], 'Destination': m['Target'], 'RW': not m['ReadOnly']} for m in mounts]
            if self.lost_create:
                self.lost_create = False
                raise engine.UpdateError('Lost create response')
            return {'Id': ident}
        if bits[0] == 'images':
            if method == 'POST':
                return {}
            return self.images[urllib.parse.unquote(bits[1])]
        if bits[0] == 'containers':
            row = self.rows[bits[1]]
            if bits[2] == 'json':
                return copy.deepcopy(row)
            if bits[2] == 'update':
                row['HostConfig'].update(copy.deepcopy(body)); return None
            if bits[2] in ('stop', 'start'):
                row['State']['Running'] = bits[2] == 'start'; return None
            if bits[2] == 'rename':
                row['Name'] = '/'+query['name'][0]; return None
        raise AssertionError((method, path))


@unittest.skipUnless(os.name == 'posix', 'Component executor targets Docker Linux; run this suite in its Linux image')
class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.trust = {'public_key': base64.b64encode(public).decode(), 'key_fingerprint': hashlib.sha256(public).hexdigest()}
        self.api = FakeDocker()
        self.registry = {'schema': 1, 'authority': 'installation', 'identity': 'fixture.local', 'components': {}, 'tenants': {}}
        self.image = 'sha256:'+'a'*64
        self.ref = 'fixture/approved@sha256:'+'b'*64
        self.add_tenant('alpha')
        self.backup_patch = patch('services.system_update_executor.execute', return_value={'verified': True, 'sha256': 'd'*64})
        self.backup_mock = self.backup_patch.start()
        self.addCleanup(self.backup_patch.stop)
        self.addCleanup(self.tmp.cleanup)
    def add_tenant(self, tenant, shared=False):
        keys = {}
        for role in ('web', 'core', 'db'):
            key = 'shared-db' if role == 'db' and shared else tenant+'-'+role
            keys[role] = key
            if key in self.registry['components']:
                continue
            project = 'shared' if key == 'shared-db' else tenant
            path = self.root/(project+'.json')
            document = json.loads(path.read_text()) if path.exists() else {'services': {}}
            document['services'][role] = {'image': self.ref, 'environment': {'FIXTURE': 'preserved'}}
            path.write_text(json.dumps(document))
            self.registry['components'][key] = {'role': role, 'project': project, 'service': role, 'compose_path': str(path), 'images': {self.ref: 'compatible-v1'}}
            self.api.add(key, project, role, self.image)
        controller = self.root/(tenant+'-controller'); controller.mkdir(exist_ok=True)
        self.registry['tenants'][tenant] = dict(keys, dependencies=list(keys.values()), controller_root=str(controller))
    def bundle(self, changes=None):
        now = dt.datetime.now(dt.timezone.utc)
        changes = changes or {'alpha-db': {'environment': {'MALLOC_ARENA_MAX': '2'}}}
        return {'schema': 1, 'protocol': 4, 'release': 'fixture-r1',
            'audience': {'authority': self.registry['authority'], 'identity': self.registry['identity']},
            'registry_revision': engine.registry_revision(self.registry),
            'issued_at': now.isoformat(), 'valid_until': (now+dt.timedelta(minutes=5)).isoformat(),
            'participants': engine.affected_components(self.registry, changes)[1],
            'changes': changes, 'data_policy': 'preserve-compatible-no-restore', 'application_versions': {}}
    def envelope(self, bundle):
        return {'bundle': bundle, 'signature': base64.b64encode(self.key.sign(engine.canonical(bundle))).decode()}
    def verify(self, bundle):
        return engine.verify_bundle(self.envelope(bundle), self.trust, self.registry)
    def probe(self, api, ident, role, context):
        self.assertTrue(api.rows[ident]['State']['Running'])
        return {'verified': True, 'version': '2.6.9', 'publisher_key': 'fixture', 'rows': []}
    def prepared(self, bundle=None):
        bundle = self.verify(bundle or self.bundle())
        plan = engine.prepare(self.api, self.registry, bundle, '1'*32, self.probe)
        folder = self.root/'job'; folder.mkdir()
        engine.write_json(folder/'plan.private.json', plan)
        engine.write_json(folder/'state.json', {'job': '1'*32, 'status': 'queued'})
        return folder, plan
    def test_signed_valid_bundle(self):
        self.verify(self.bundle())
    def test_tampered_signature(self):
        value = self.envelope(self.bundle()); value['bundle']['release'] = 'changed'
        with self.assertRaises(Exception): engine.verify_bundle(value, self.trust, self.registry)
    def test_wrong_audience(self):
        b = self.bundle(); b['audience']['identity'] = 'another'
        with self.assertRaises(engine.UpdateError): self.verify(b)
    def test_stale_registry(self):
        b = self.bundle(); b['registry_revision'] = '0'*64
        with self.assertRaises(engine.UpdateError): self.verify(b)
    def test_expired_or_long_authorization(self):
        for seconds in (-1, 301):
            b = self.bundle(); b['valid_until'] = (dt.datetime.fromisoformat(b['issued_at'])+dt.timedelta(seconds=seconds)).isoformat()
            with self.assertRaises(engine.UpdateError): self.verify(b)
    def test_free_command_or_environment_rejected(self):
        for change in ({'command': ['rm']}, {'environment': {'LD_PRELOAD': '/evil'}}, {'mariadb_options': {'skip-grant-tables': 1}}):
            with self.assertRaises(engine.UpdateError): self.verify(self.bundle({'alpha-db': change}))
    def test_role_confusion(self):
        with self.assertRaises(engine.UpdateError): self.verify(self.bundle({'alpha-web': {'environment': {'MALLOC_ARENA_MAX': '2'}}}))
    def test_image_requires_host_compatibility(self):
        with self.assertRaises(engine.UpdateError): self.verify(self.bundle({'alpha-db': {'image': 'mariadb@sha256:'+'c'*64}}))
    def test_db_mount_required(self):
        self.api.rows[next(k for k, v in self.api.rows.items() if v['Name'] == '/alpha-db')]['Mounts'] = []
        with self.assertRaises(engine.UpdateError): self.prepared()
    def test_scope_unknown_fails_closed(self):
        for row in self.api.rows.values(): row['Config']['Labels'].pop('max.db.scope')
        with self.assertRaises(engine.UpdateError): self.prepared()
    def test_shared_engine_cannot_be_mutated_by_tenant(self):
        for row in self.api.rows.values(): row['Config']['Labels']['max.db.scope'] = 'shared'
        with self.assertRaises(engine.UpdateError): self.prepared()
    def test_shared_participants_must_be_complete(self):
        self.registry['authority'] = 'host'; self.add_tenant('beta', shared=True); self.add_tenant('gamma', shared=True)
        b = self.bundle({'shared-db': {'environment': {'MALLOC_ARENA_MAX': '2'}}}); b['participants'] = ['beta']
        with self.assertRaises(engine.UpdateError): self.verify(b)
    def test_every_shared_tenant_backup_required(self):
        self.registry['authority'] = 'host'; self.add_tenant('beta', shared=True); self.add_tenant('gamma', shared=True)
        self.backup_mock.side_effect = [{'verified': True, 'sha256': 'a'}, {'verified': False}]
        with self.assertRaises(engine.UpdateError): self.prepared(self.bundle({'shared-db': {'environment': {'MALLOC_ARENA_MAX': '2'}}}))
        self.assertFalse(any('/stop?' in path for _, path in self.api.calls))
    def test_update_db_and_clients_without_data_restore(self):
        folder, plan = self.prepared()
        result = engine.run(folder, self.api, self.probe)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(result['records']), {'alpha-db', 'alpha-core', 'alpha-web'})
        new = self.api.rows[result['records']['alpha-db']['new_id']]
        self.assertIn('MALLOC_ARENA_MAX=2', new['Config']['Env'])
        self.assertEqual(new['Mounts'][0]['Name'], 'alpha-db-data')
        self.assertFalse(self.api.rows[plan['old']['alpha-db']['Id']]['State']['Running'])
        count = len(self.api.calls); engine.run(folder, self.api, self.probe); self.assertEqual(count, len(self.api.calls))
    def test_lost_create_rolls_back_once(self):
        folder, plan = self.prepared(); self.api.lost_create = True
        result = engine.run(folder, self.api, self.probe)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertTrue(all(self.api.rows[v['Id']]['State']['Running'] for v in plan['old'].values()))
    def test_failed_probe_rolls_back_and_checks_originals(self):
        folder, plan = self.prepared()
        def probe(api, ident, role, context):
            if ident not in {v['Id'] for v in plan['old'].values()}:
                raise engine.UpdateError('Failed candidate')
            return self.probe(api, ident, role, context)
        with patch.object(engine, 'wait_probe', side_effect=lambda api, ident, role, context, p: p(api, ident, role, context)):
            result = engine.run(folder, self.api, probe)
        self.assertEqual(result['status'], 'rolled_back')
    def test_rollback_failure_requires_recovery(self):
        folder, plan = self.prepared()
        with patch.object(engine, 'wait_probe', side_effect=engine.UpdateError('Both probes fail')):
            self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'recovery_required')
    def test_interruption_not_replayed(self):
        folder, _ = self.prepared(); engine.write_json(folder/'state.json', {'status': 'running', 'switch_attempted': True})
        count = len(self.api.calls)
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'recovery_required')
        self.assertEqual(count, len(self.api.calls))
    def test_foreign_compose_edit_prevents_stop(self):
        folder, plan = self.prepared(); Path(self.registry['components']['alpha-db']['compose_path']).write_text('foreign')
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'error')
        self.assertFalse(any('/stop?' in path for _, path in self.api.calls))
    def test_next_snapshot_ignores_owned_stopped_history(self):
        folder, plan = self.prepared(); self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'completed')
        self.assertEqual(len(engine.snapshot(self.api, self.registry)), 3)
    def test_host_updates_shared_db_without_touching_unrelated_tenant(self):
        self.registry['authority'] = 'host'; self.add_tenant('beta', shared=True); self.add_tenant('gamma', shared=True)
        bundle = self.bundle({'shared-db': {'environment': {'MALLOC_ARENA_MAX': '2'}}})
        folder, plan = self.prepared(bundle)
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'completed')
        self.assertFalse(any(plan['old'][key]['Id'] in path and method == 'POST' for method, path in self.api.calls for key in ('alpha-web', 'alpha-core', 'alpha-db')))
    def test_barrier_rejects_active_legacy_helper(self):
        cfg = self.registry['tenants']['alpha']; root = Path(cfg['controller_root'])
        job = 'c'*32; folder = root/'jobs'/job; folder.mkdir(parents=True)
        engine.write_json(root/'current.private.json', {'job': job})
        engine.write_json(folder/'state.json', {'status': 'running'})
        with self.assertRaises(engine.UpdateError):
            with engine.tenant_barriers(self.registry, ['alpha']): pass
    def test_barrier_interlocks_with_legacy_dispatch(self):
        import fcntl
        path = Path(self.registry['tenants']['alpha']['controller_root'])/'dispatch.lock'
        with engine.tenant_barriers(self.registry, ['alpha']):
            with path.open('a') as other:
                with self.assertRaises(BlockingIOError): fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def test_autoheal_is_quiesced_before_db_and_clients(self):
        key = 'alpha-watchdog'; path = self.root/'alpha.json'; data = json.loads(path.read_text()); data['services']['autoheal'] = {'image': self.ref}; path.write_text(json.dumps(data))
        self.registry['components'][key] = {'role': 'autoheal', 'project': 'alpha', 'service': 'autoheal', 'compose_path': str(path), 'images': {self.ref: 'compatible-v1'}}
        self.registry['tenants']['alpha']['dependencies'].append(key); self.api.add(key, 'alpha', 'autoheal', self.image)
        folder, plan = self.prepared(); self.assertEqual(plan['order'][-1], key)
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'completed')
        stops = [path for method, path in self.api.calls if method == 'POST' and '/stop?' in path]
        self.assertIn(plan['old'][key]['Id'], stops[0])
    def test_config_command_preserves_other_settings(self):
        old = next(v for v in self.api.rows.values() if v['Name'] == '/alpha-db')
        old['Config']['Cmd'] = ['mariadbd', '--max-connections=151', '--key_buffer_size=134217728']
        result = engine.change_spec(old, self.image, {'mariadb_options': {'key-buffer-size': 8388608}})
        self.assertEqual(result['Cmd'], ['mariadbd', '--max-connections=151', '--key-buffer-size=8388608'])
        old['Config']['Cmd'] = ['mariadbd', '--key-buffer-size', '134217728']
        with self.assertRaises(engine.UpdateError): engine.change_spec(old, self.image, {'mariadb_options': {'key-buffer-size': 8388608}})
    def test_all_component_image_roles_use_same_executor_contract(self):
        for role in ('l2tp', 'autoheal'):
            key = 'alpha-'+role; path = self.root/'alpha.json'; data = json.loads(path.read_text()); data['services'][role] = {'image': self.ref}; path.write_text(json.dumps(data))
            self.registry['components'][key] = {'role': role, 'project': 'alpha', 'service': role, 'compose_path': str(path), 'images': {self.ref: 'compatible-v1'}}
            self.registry['tenants']['alpha']['dependencies'].append(key); self.api.add(key, 'alpha', role, self.image)
        candidate = 'sha256:'+'c'*64
        self.api.images[self.image] = {'Id': self.image, 'RepoDigests': [self.ref]}
        self.api.images[self.ref] = {'Id': candidate, 'RepoDigests': [self.ref]}
        bundle = self.bundle({k: {'image': self.ref} for k in self.registry['components']})
        bundle['application_versions'] = {'alpha-web': '2.6.9'}
        folder, plan = self.prepared(bundle)
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'completed')
        self.assertEqual(set(plan['specs']), set(self.registry['components']))
    def test_incompatible_current_db_image_is_rejected_before_stop(self):
        self.api.images[self.image] = {'Id': self.image, 'RepoDigests': ['other@sha256:'+'e'*64]}
        with self.assertRaises(engine.UpdateError): self.prepared(self.bundle({'alpha-db': {'image': self.ref}}))
        self.assertFalse(any('/stop?' in path for _, path in self.api.calls))
    def test_submit_is_signed_and_release_replay_is_rejected(self):
        engine.write_json(self.root/'component-registry.private.json', self.registry)
        engine.write_json(self.root/'component-trust.private.json', self.trust)
        envelope = self.envelope(self.bundle())
        self.assertEqual(engine.submit(self.root, envelope, self.api, self.probe)['status'], 'completed')
        previous = len(self.api.calls)
        with self.assertRaises(engine.UpdateError): engine.submit(self.root, envelope, self.api, self.probe)
        self.assertEqual(previous, len(self.api.calls))
    def test_crlf_compose_preserved_in_rollback(self):
        path = Path(self.registry['components']['alpha-web']['compose_path'])
        path.write_bytes(b'{\r\n"services":{"web":{},"core":{},"db":{}}\r\n}')
        folder, plan = self.prepared()
        self.api.lost_create = True
        self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'rolled_back')
        self.assertEqual(path.read_bytes().decode(), plan['documents'][str(path)]['before'])
    def test_lost_create_does_not_require_ip_of_never_started_candidate(self):
        folder, plan = self.prepared(); self.api.lost_create = True
        original = engine.endpoints_for
        def endpoints(row):
            if row['Id'] not in {v['Id'] for v in plan['old'].values()}:
                raise engine.UpdateError('Never-started candidate has no IP')
            return original(row)
        with patch.object(engine, 'endpoints_for', side_effect=endpoints):
            self.assertEqual(engine.run(folder, self.api, self.probe)['status'], 'rolled_back')
    def test_db_probe_requires_authenticated_query_not_ping(self):
        calls = []
        def api(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith('/exec'): return {'Id': 'fixture-exec'}
            if path.endswith('/json'): return {'ExitCode': 1}
        with self.assertRaises(engine.UpdateError): engine.default_probe(api, 'fixture-db', 'db', {})
        command = calls[0][2]['Cmd'][-1]
        self.assertIn('SELECT 1', command)
        self.assertNotIn('mariadb-admin', command)
        self.assertIn('MYSQL_PWD=', command)
    def test_web_identity_storage_is_required_before_backup(self):
        web = next(v for v in self.api.rows.values() if v['Name'] == '/alpha-web'); web['Mounts'] = []
        with self.assertRaises(engine.UpdateError): self.prepared()
        self.assertFalse(self.backup_mock.called)
    def test_runtime_source_override_rejected(self):
        web = next(v for v in self.api.rows.values() if v['Name'] == '/alpha-web'); web['Mounts'].append({'Type': 'bind', 'Source': '/fixture/code', 'Destination': '/app/services', 'RW': False})
        with self.assertRaises(engine.UpdateError): self.prepared()
    def test_web_only_change_also_quiesces_its_watchdog(self):
        key = 'alpha-autoheal'; path = self.root/'alpha.json'; data = json.loads(path.read_text()); data['services']['autoheal'] = {'image': self.ref}; path.write_text(json.dumps(data))
        self.registry['components'][key] = {'role': 'autoheal', 'project': 'alpha', 'service': 'autoheal', 'compose_path': str(path), 'images': {self.ref: 'compatible-v1'}}
        self.registry['tenants']['alpha']['dependencies'].append(key); self.api.add(key, 'alpha', 'autoheal', self.image)
        changes = {'alpha-web': {'image': self.ref}}
        affected, participants = engine.affected_components(self.registry, changes)
        self.assertEqual(affected, {'alpha-web', key}); self.assertEqual(participants, ['alpha'])


    def test_unverifiable_original_component_rejected_before_backup_or_stop(self):
        bundle = self.verify(self.bundle())
        def probe(api, ident, role, context):
            if role == 'core':
                raise engine.UpdateError('Retained component cannot be verified')
            return self.probe(api, ident, role, context)
        with self.assertRaises(engine.UpdateError):
            engine.prepare(self.api, self.registry, bundle, '1'*32, probe)
        self.assertFalse(self.backup_mock.called)
        self.assertFalse(any('/stop?' in path for _, path in self.api.calls))

    def test_transport_deadline_exceeds_docker_stop_grace_in_apply_and_rollback(self):
        folder, plan = self.prepared()
        old_ids = {row['Id'] for row in plan['old'].values()}
        stops = []
        def api(method, path, body=None, timeout=30):
            if method == 'POST' and '/stop?' in path:
                grace = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['t'][0])
                self.assertGreater(timeout, grace)
                stops.append(path.split('/')[2] in old_ids)
            return self.api(method, path, body, timeout)
        def probe(adapter, ident, role, context):
            if ident not in old_ids:
                raise engine.UpdateError('Candidate rejected')
            return self.probe(self.api, ident, role, context)
        with patch.object(engine, 'wait_probe', side_effect=lambda api, ident, role, context, p: p(api, ident, role, context)):
            self.assertEqual(engine.run(folder, api, probe)['status'], 'rolled_back')
        self.assertIn(True, stops)
        self.assertIn(False, stops)

    def test_default_web_probe_rejects_invalid_license_lease_or_integrity(self):
        good = {'verified': True, 'license': True, 'lease': True,
                'version': '2.6.9', 'publisher_key': 'trusted', 'rows': []}
        for field in ('verified', 'license', 'lease'):
            with self.subTest(field=field):
                value = dict(good, **{field: False})
                with patch('services.system_update_executor.execute', return_value=value):
                    with self.assertRaises(engine.UpdateError):
                        engine.default_probe(self.api, 'fixture', 'web', {})

    def test_default_web_probe_rejects_changed_identity_publisher_or_version(self):
        good = {'verified': True, 'license': True, 'lease': True,
                'version': '2.6.9', 'publisher_key': 'trusted', 'rows': [],
                'identity': {'machine': 'm', 'instance': 'i', 'license_key': 'k', 'web_secret': 's'}}
        for field in ('machine', 'instance', 'license_key', 'web_secret', 'publisher_key', 'version'):
            with self.subTest(field=field):
                value, identity = dict(good), dict(good['identity'])
                if field in identity:
                    identity[field] = 'changed'
                else:
                    value[field] = 'changed'
                with patch('services.system_update_executor.execute', side_effect=[value, identity]):
                    with self.assertRaises(engine.UpdateError):
                        engine.default_probe(self.api, 'fixture', 'web', {'baseline': good})

    def test_default_web_probe_checks_accounting_and_expected_next_version(self):
        before = {'verified': True, 'license': True, 'lease': True, 'version': '2.6.9',
                  'publisher_key': 'trusted', 'rows': [{'radacctid': 1}], 'identity': {'instance': 'same'}}
        after = dict(before, version='2.7.0')
        for preserved in (False, True):
            with self.subTest(preserved=preserved):
                with patch('services.system_update_executor.execute', side_effect=[after.copy(), before['identity'], {'preserved': preserved}]):
                    context = {'baseline': before, 'expected_version': '2.7.0'}
                    if preserved:
                        self.assertEqual(engine.default_probe(self.api, 'fixture', 'web', context)['version'], '2.7.0')
                    else:
                        with self.assertRaises(engine.UpdateError):
                            engine.default_probe(self.api, 'fixture', 'web', context)

    def test_lost_stop_start_and_rename_replies_restore_originals(self):
        folder, plan = self.prepared()
        original_rows = copy.deepcopy(self.api.rows)
        for action in ('stop', 'start', 'rename', 'update'):
            with self.subTest(action=action):
                self.api.rows = copy.deepcopy(original_rows)
                engine.write_json(folder/'state.json', {'job': plan['job'], 'status': 'queued'})
                hit = [False]
                def adapter(method, path, body=None, timeout=30):
                    result = self.api(method, path, body, timeout)
                    route = urllib.parse.urlsplit(path).path
                    if method == 'POST' and route.startswith('/containers/') and route.endswith('/'+action) and not hit[0]:
                        hit[0] = True
                        raise engine.UpdateError('Lost Docker mutation response')
                    return result
                result = engine.run(folder, adapter, lambda api, ident, role, context: self.probe(self.api, ident, role, context))
                self.assertTrue(hit[0])
                self.assertEqual(result['status'], 'rolled_back')
                for row in plan['old'].values():
                    self.assertEqual(self.api.rows[row['Id']]['Name'], row['Name'])
                    self.assertTrue(self.api.rows[row['Id']]['State']['Running'])

    def test_transport_and_watchdog_need_installed_health_contract(self):
        for role in ('l2tp', 'autoheal'):
            ident = self.api.add('fixture-'+role, 'alpha', role, self.image)
            with self.subTest(role=role):
                with self.assertRaises(engine.UpdateError):
                    engine.default_probe(self.api, ident, role, {})
                self.api.rows[ident]['Config']['Healthcheck'] = {'Test': ['CMD', 'true']}
                self.assertTrue(engine.default_probe(self.api, ident, role, {})['verified'])
                self.api.rows[ident]['State']['Health']['Status'] = 'unhealthy'
                with self.assertRaises(engine.UpdateError):
                    engine.default_probe(self.api, ident, role, {})


if __name__ == '__main__':
    unittest.main()
