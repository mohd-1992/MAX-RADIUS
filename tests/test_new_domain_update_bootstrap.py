"""Isolated old-panel creation hook checks. No server, customer or production DB."""
import hashlib
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


hook=load('new_domain_updates','deploy/hosted-max/new_domain_updates.py')
provision=load('new_domain_provision','deploy/hosted-max/provision_update_controller.py')


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
        self.backend=self.root/'backend';self.backend.mkdir()
        hashes={}
        for name in ('platform_store.py','release_center.py','local_releases.py','password_policy.py'):
            p=self.backend/name;p.write_text('# fixture source\n');hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
        self.store=self.root/'control';self.store.mkdir();(self.store/'vault.key').write_bytes(b'fixture-vault-not-production')
        with contextlib.closing(sqlite3.connect(self.store/'platform.sqlite3')) as db:
            for name in ('update_targets','receipts','audit'):db.execute('CREATE TABLE '+name+'(id TEXT)')
        self.cfg={'tenant_root':str(self.root/'tenants'),'state_root':str(self.root/'private'),'socket_root':str(self.root/'sockets'),
                  'base_domain':'example.test','control_backend':str(self.backend),'control_store':str(self.store),
                  'control_source_sha256':hashes,'publisher_key_sha256':'f'*64,'key_fingerprint':hook.PUBLIC_TRUST}
        self.settings=self.root/'settings.json';self.settings.write_text(json.dumps(self.cfg));self.setting_patch=patch.object(hook,'SETTINGS',self.settings);self.setting_patch.start()
        self.path=self.root/'tenants'/'fresh';self.path.mkdir(parents=True)
        self.meta={'subdomain':'fresh','tunnel_mode':'central','instance_uuid':'INST-ABCDEF1234'}
        (self.path/'tenant.json').write_text(json.dumps(self.meta))
        self.reference='mohd777/max-radius-web@sha256:'+'a'*64;self.image='sha256:'+'b'*64
        (self.path/'creation-release.json').write_text(json.dumps({'web':{'reference':self.reference,'image_id':self.image}}))
        self.compose={'services':{'web':{'image':self.reference,'environment':{},'volumes':['./storage:/app/storage','/metadata.sock:/var/run/docker.sock:ro']},
                                  'radius_core':{'image':'core-original'},'db':{'image':'db-original'}}}
        (self.path/'docker-compose.yml').write_text(json.dumps(self.compose))
        self.enrollment={'kind':'domain','identity':'fresh.example.test','reader_id':'reader-fixture','token':'fixture-reader-credential-only'}
        self.calls=[]
    def tearDown(self):self.setting_patch.stop();self.t.cleanup()
    def command(self,args,timeout=120,input=None):
        self.calls.append((args,input))
        if args[:3]==['docker','ps','-aq']:return ''
        if args[:3]==['docker','image','inspect']:
            return json.dumps([{'Id':self.image,'Config':{'Labels':{'org.opencontainers.image.version':'2.6.4','max.release.channel':'control-v1'}}}])
        if args[0]==sys.executable:return json.dumps(self.enrollment)
        if args[:2]==['docker','run']:
            if input:
                p=json.loads(input);return json.dumps(provision.provision(p['path'],p['name'],p['private'],p['socket'],enrollment=p['enrollment']))
            return json.dumps({'verified':True,'key':self.cfg['publisher_key_sha256'],'channel_key':hook.PUBLIC_TRUST})
        if args[:2]==['docker','compose']:
            import yaml
            self.document=yaml.safe_load((self.path/'docker-compose.yml').read_text());return ''
        if args[:2]==['docker','inspect']:return json.dumps([{'Image':self.image}])
        if args[:2]==['docker','exec']:return json.dumps({'verified':True,'version':'2.6.4','install_started':False})
        raise AssertionError(args)
    def prepare(self):
        with patch.object(hook,'run',side_effect=self.command):hook.prepare_new_domain_updates(self.path,'fresh')
    def test_preflight_does_not_write_control_or_generate_keys(self):
        before={p.name:p.read_bytes() for p in self.store.iterdir()}
        hook.preflight_new_domain_updates()
        self.assertEqual({p.name:p.read_bytes() for p in self.store.iterdir()},before)
    def test_changed_control_contract_fails_closed(self):
        (self.backend/'release_center.py').write_text('changed')
        with self.assertRaises(hook.BootstrapError):hook.preflight_new_domain_updates()
    def test_missing_vault_is_not_regenerated(self):
        (self.store/'vault.key').unlink()
        with self.assertRaises(hook.BootstrapError):hook.preflight_new_domain_updates()
        self.assertFalse((self.store/'vault.key').exists())
    def test_existing_or_failed_creation_cannot_be_bootstrapped(self):
        for name in ('_panel_create_failed.json','update-controller-provisioned.json'):
            p=self.path/name;p.write_text('{}')
            with self.assertRaises(hook.BootstrapError):self.prepare()
            p.unlink()
        self.assertEqual(self.calls,[])
    def test_foreign_tenant_path_rejected_before_docker(self):
        with self.assertRaises(hook.BootstrapError):
            with patch.object(hook,'run',side_effect=AssertionError()):hook.prepare_new_domain_updates(self.path,'other')
    def test_existing_container_blocks_enrollment(self):
        with patch.object(hook,'run',return_value='existing-container'):
            with self.assertRaises(hook.BootstrapError):hook.prepare_new_domain_updates(self.path,'fresh')
        self.assertFalse((self.root/'private').exists())
    def test_unpinned_creation_image_rejected(self):
        (self.path/'creation-release.json').write_text(json.dumps({'web':{'reference':'mohd777/max-radius-web:latest','image_id':self.image}}))
        with self.assertRaises(hook.BootstrapError):self.prepare()
        self.assertEqual(self.calls,[])
    def test_new_creation_provisions_target_controller_without_changing_db_or_core(self):
        self.prepare();document=self.document
        self.assertEqual(document['services']['db'],self.compose['services']['db'])
        self.assertEqual(document['services']['radius_core'],self.compose['services']['radius_core'])
        agent=document['services']['update_controller'];self.assertEqual(agent['container_name'],'max_radius_fresh_updates');self.assertEqual(agent['network_mode'],'bridge')
        config=json.loads((self.root/'private/fresh/update-channel.private.json').read_text())
        self.assertEqual(config['reader_id'],self.enrollment['reader_id']);self.assertEqual(config['identity'],'fresh.example.test')
        self.assertNotIn(self.enrollment['token'],(self.path/'docker-compose.yml').read_text())
        self.assertNotIn(self.enrollment['token'],(self.root/'private/fresh.bootstrap.private.json').read_text())
        self.assertEqual(document['services']['web']['environment']['MAX_UPDATE_SOCKET'],'/max-update-socket/controller.sock')
    def test_completed_preparation_is_not_replayed(self):
        self.prepare();n=len(self.calls)
        with self.assertRaises(hook.BootstrapError):self.prepare()
        self.assertEqual(len(self.calls),n)
    def test_ambiguous_enrollment_does_not_repeat_and_does_not_leak_exception(self):
        original=self.command
        def broken(args,**kw):
            if args[0]==sys.executable:raise RuntimeError(self.enrollment['token'])
            return original(args,**kw)
        with patch.object(hook,'run',side_effect=broken):
            with self.assertRaises(hook.BootstrapError) as caught:hook.prepare_new_domain_updates(self.path,'fresh')
        self.assertNotIn(self.enrollment['token'],str(caught.exception))
        receipt=json.loads((self.root/'private/fresh.bootstrap.private.json').read_text());self.assertEqual(receipt['stage'],'review_required')
        with self.assertRaises(hook.BootstrapError):self.prepare()
    def test_ready_check_is_read_only_and_never_applies_update(self):
        self.prepare();self.calls=[]
        with patch.object(hook,'run',side_effect=self.command):hook.verify_new_domain_updates(self.path,'fresh')
        proof=json.loads((self.root/'private/fresh/creation-ready.private.json').read_text())
        self.assertTrue(proof['targeted_feed_verified']);self.assertFalse(proof['install_started'])
        self.assertFalse(any('apply' in str(args) for args,_ in self.calls))
    def test_failed_readiness_is_not_marked_ready(self):
        self.prepare();original=self.command
        def broken(args,**kw):
            if args[:2]==['docker','exec']:raise hook.BootstrapError('fixture unavailable')
            return original(args,**kw)
        with patch.object(hook,'run',side_effect=broken),patch.object(hook.time,'sleep',return_value=None):
            with self.assertRaises(hook.BootstrapError):hook.verify_new_domain_updates(self.path,'fresh')
        self.assertFalse((self.root/'private/fresh/creation-ready.private.json').exists())
    def test_nonzero_subprocess_does_not_expose_stdout_stderr(self):
        value=types.SimpleNamespace(returncode=1,stdout=self.enrollment['token'],stderr=self.enrollment['token'])
        with patch.object(hook.subprocess,'run',return_value=value):
            with self.assertRaises(hook.BootstrapError) as caught:hook.run(['fixture'])
        self.assertNotIn(self.enrollment['token'],str(caught.exception))


if __name__=='__main__':unittest.main()
