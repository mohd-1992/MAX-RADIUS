"""Linux-only fault/scope tests for the pilot host. No Docker/systemd operations."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64

SOURCE=Path(__file__).resolve().parents[1]/'deploy/hosted-max'
def load(name):
    spec=importlib.util.spec_from_file_location(name,SOURCE/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
host=load('pilot_host_engine');fleet=load('pilot_fleet_controller')


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.channel=fleet.Channel(self.root)
        self.plan={'domain':'4k.max-net.net','project':'4k'};raw=json.dumps(self.plan).encode();(self.root/'approved-plan.private.json').write_bytes(raw)
        self.config=dict(domain='4k.max-net.net',project='4k',enabled=True,release='4k-pilot',instance='a'*32,images={'web':'sha256:'+'b'*64,'core':'sha256:'+'c'*64},plan_sha256=hashlib.sha256(raw).hexdigest(),catalogue_path=str(self.root/'catalogue.json'))
        (self.root/'configuration.private.json').write_text(json.dumps(self.config))
        self.key=Ed25519PrivateKey.generate();(self.root/'release-public.pem').write_bytes(self.key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
        self.item={k:self.config[k] for k in ('domain','release','instance','images','plan_sha256')};self.item.update(version='2.6.3',components=['web','core'],automatic_install=False,changelog=['fixture'],release_date='2026-10-22');self.sign()
    def tearDown(self):self.temp.cleanup()
    def sign(self):
        value=copy.deepcopy(self.item);value['signature']=base64.b64encode(self.key.sign(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())).decode();(self.root/'catalogue.json').write_text(json.dumps(value))
    def test_other_tenant_route_or_method_denied(self):
        for tenant,path,method in [('max',fleet.PREFIX+'check','GET'),('as',fleet.PREFIX+'apply','POST'),('4k','/_max_pilot/max/check','GET'),('4k',fleet.PREFIX+'check','POST'),('4k',fleet.PREFIX+'apply','DELETE')]:
            self.assertEqual(self.channel.dispatch(tenant,method,path)[0],403)
    def test_unsigned_or_altered_manifest_rejected(self):
        value=json.loads((self.root/'catalogue.json').read_text());value['domain']='max.max-net.net';(self.root/'catalogue.json').write_text(json.dumps(value))
        with self.assertRaises(Exception):self.channel.release()
    def test_even_valid_signature_cannot_change_instance_or_images(self):
        for field,value in [('domain','max.max-net.net'),('instance','f'*32),('components',['web','core','db']),('images',{'web':'latest','core':'latest'}),('automatic_install',True)]:
            old=copy.deepcopy(self.item);self.item[field]=value;self.sign()
            with self.subTest(field=field),self.assertRaises(ValueError):self.channel.release()
            self.item=old;self.sign()
    def test_check_records_delivery_not_installation(self):
        with patch.object(fleet.subprocess,'run',side_effect=AssertionError('No dispatch on GET')):
            result=self.channel.check()
        self.assertTrue(result['has_update']);self.assertFalse(json.loads((self.root/'delivery-receipt.json').read_text())['installed']);self.assertFalse((self.root/'current.private.json').exists())
    def test_claim_precedes_dispatch_and_repeated_click_does_not_dispatch(self):
        def dispatch(*args,**kw):
            pointer=json.loads((self.root/'current.private.json').read_text());self.assertTrue((self.root/'jobs'/pointer['job']/'plan.private.json').exists());return Mock(returncode=0)
        with patch.object(fleet.subprocess,'run',side_effect=dispatch) as call:
            first=self.channel.submit({'version':'2.6.3','backup_first':True});second=self.channel.submit({'version':'2.6.3','backup_first':True})
        self.assertEqual(first['job'],second['job']);self.assertEqual(call.call_count,2)
    def test_uncertain_dispatch_remains_claimed_without_retry(self):
        with patch.object(fleet.subprocess,'run',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):self.channel.submit({'version':'2.6.3','backup_first':True})
        with patch.object(fleet.subprocess,'run',side_effect=AssertionError('No retry')):
            receipt=self.channel.submit({'version':'2.6.3','backup_first':True})
        self.assertTrue(receipt['success']);self.assertEqual(self.channel.progress()['status'],'unknown')
    def test_request_cannot_supply_images_domain_or_disable_backup(self):
        for body in ({'version':'latest','backup_first':True},{'version':'2.6.3','backup_first':False},{'version':'2.6.3','backup_first':True,'domain':'max.max-net.net'}):
            with self.assertRaises(ValueError):self.channel.submit(body)
        self.assertFalse((self.root/'current.private.json').exists())


class EngineFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.root=self.base/'jobs'/('a'*32);self.root.mkdir(parents=True)
        self.old={r:{'Id':str(n)*64,'Name':'/fixture-'+r,'Image':'sha256:'+str(n)*64,'Config':{'Env':[],'Labels':{'com.docker.compose.project':'4k','com.docker.compose.service':host.SERVICES[r]}},'HostConfig':{'RestartPolicy':{'Name':'unless-stopped'}},'Mounts':[],'NetworkSettings':{'Networks':{}}} for r,n in [('web',1),('core',2)]}
        self.plan=dict(job='a'*32,domain='4k.max-net.net',project='4k',release='fixture',old=self.old,images={'web':'sha256:'+'b'*64,'core':'sha256:'+'c'*64})
        compose=self.base/'compose.json';compose.write_text(json.dumps({'services':{'web':{},'radius_core':{},'db':{}}}))
        self.plan.update(compose_path=str(compose),compose_sha256=hashlib.sha256(compose.read_bytes()).hexdigest(),compatible_core='sha256:'+'e'*64)
        (self.root/'plan.private.json').write_text(json.dumps(self.plan));host.write(self.root/'state.json',{'status':'queued','services':{r:{} for r in host.ROLES}});self.e=host.Engine(self.root)
    def tearDown(self):self.temp.cleanup()
    def test_extra_database_component_rejected(self):
        self.plan['images']['db']='sha256:'+'d'*64;(self.root/'plan.private.json').write_text(json.dumps(self.plan))
        with self.assertRaises(ValueError):host.Engine(self.root)
    def test_wrong_domain_rejected(self):
        self.plan['domain']='max.max-net.net';(self.root/'plan.private.json').write_text(json.dumps(self.plan))
        with self.assertRaises(ValueError):host.Engine(self.root)
    def test_uncertain_create_is_resolved_once_by_name_image_and_job(self):
        actual={'Id':'d'*64,'Image':self.plan['images']['web'],'Config':{'Labels':{'max.pilot.job':'a'*32}}}
        self.e.api=Mock(side_effect=TimeoutError());self.e.inspect=Mock(return_value=actual);self.e.spec=Mock(return_value={});self.e.docker=Mock();self.e.wait=Mock()
        self.assertEqual(self.e.create('web',self.plan['images']['web']),'d'*64);self.e.api.assert_called_once();self.e.docker.assert_called_once_with('start','d'*64)
    def test_uncertain_create_cannot_adopt_foreign_job(self):
        self.e.api=Mock(side_effect=TimeoutError());self.e.inspect=Mock(return_value={'Id':'d'*64,'Image':self.plan['images']['web'],'Config':{'Labels':{'max.pilot.job':'foreign'}}});self.e.spec=Mock(return_value={});self.e.docker=Mock()
        with self.assertRaises(TimeoutError):self.e.create('web',self.plan['images']['web'])
        self.e.docker.assert_not_called()
    def test_failed_candidate_and_failed_rollback_never_report_success(self):
        self.e.validate=Mock();self.e.probe=Mock();self.e.prepare_backup=Mock();self.e.preserve=Mock();self.e.migrate=Mock();self.e.create=Mock(side_effect=RuntimeError('fixture bad candidate'));self.e.rollback=Mock(side_effect=RuntimeError('fixture failed restoration'))
        with self.assertRaises(RuntimeError):self.e.apply()
        state=json.loads((self.root/'state.json').read_text());self.assertEqual(state['status'],'recovery_required');self.assertLess(state['percent'],100);self.assertFalse((self.root/'ACCEPTED').exists())
    def test_accepted_or_resolved_rollback_is_noop(self):
        self.e.docker=Mock(side_effect=AssertionError('No operations after resolution'))
        for status in ('rolled_back','recovery_required'):
            self.e.state['status']=status;self.e.rollback()
        self.e.state['status']='completed';(self.root/'ACCEPTED').write_text('accepted');self.e.rollback()

    def test_failed_switch_keeps_persistent_compatible_core_after_rollback(self):
        self.e.validate=Mock();self.e.probe=Mock();self.e.prepare_backup=Mock();self.e.preserve=Mock(side_effect=RuntimeError('fixture interruption'))
        self.e.rollback=Mock(side_effect=self.e.restore_compose)
        with self.assertRaises(RuntimeError):self.e.apply()
        self.assertTrue((self.root/'compose-original.private').exists())
        restored=json.loads(Path(self.plan['compose_path']).read_text())
        self.assertEqual(restored['services']['radius_core']['image'],self.plan['compatible_core'])
        self.assertEqual(restored['services']['radius_core']['pull_policy'],'never')

    def test_unrelated_verification_is_one_frozen_batch_and_missing_identity_fails(self):
        self.e.state['unrelated']={'1'*64:{},'2'*64:{}};self.e.docker=Mock(return_value='[]')
        with self.assertRaises(RuntimeError):self.e.verify_unrelated()
        self.e.docker.assert_called_once_with('inspect','1'*64,'2'*64)


if __name__=='__main__':unittest.main()
