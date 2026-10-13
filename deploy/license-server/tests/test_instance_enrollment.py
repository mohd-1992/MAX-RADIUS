"""Isolated SQLite/Flask and Ed25519 activation regressions; no live license writes."""
import os,ast,base64,concurrent.futures,datetime,json,secrets,sqlite3,sys,tempfile,time,types,unittest,threading,importlib.util,io
from functools import wraps
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
_tmp=tempfile.TemporaryDirectory();cfg=types.ModuleType('config');cfg.DATA_DIR=Path(_tmp.name);cfg.DB_PATH=cfg.DATA_DIR/'test.sqlite3';sys.modules['config']=cfg
from database.db import get_db
from core import heartbeat_security as h
from core import instance_enrollment as e
from core.license_protocol import canonical
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from flask import Flask,jsonify,session,redirect,url_for,request,flash,send_file
MASTER=Ed25519PrivateKey.generate()
class Tests(unittest.TestCase):
 def setUp(self):
  cfg.DB_PATH=cfg.DATA_DIR/(secrets.token_hex(8)+'.sqlite3');c=get_db();c.executescript('''CREATE TABLE master_licenses(license_id TEXT PRIMARY KEY,status TEXT,expires_at TEXT,hardware_id TEXT,instance_id TEXT,max_instances INT,raw_json TEXT);CREATE TABLE master_instances(instance_uuid TEXT PRIMARY KEY,status TEXT);CREATE TABLE master_blacklist(target_type TEXT,target_value TEXT);CREATE TABLE master_audit_logs(action TEXT,entity_type TEXT,entity_id TEXT,details TEXT);''');c.execute('INSERT INTO master_licenses VALUES(?,?,?,?,?,?,?)',('LIC','active','2099-01-01T00:00:00Z','ANY','ANY',1,json.dumps({'payload':{'license_id':'LIC'},'signature':'unchanged'})));c.commit();c.close();self.app=Flask('enroll-test');self.app.config['TESTING']=True;h.install_protocol(self.app)
  self.app.add_url_rule('/api/v1/heartbeat/ping','heartbeat',lambda:jsonify(valid=True,status='active'),methods=['POST']);self.client=self.app.test_client();self.key=Ed25519PrivateKey.generate()
 def sql(self,q,args=()):
  c=get_db()
  try:r=c.execute(q,args).fetchall();c.commit();return r
  finally:c.close()
 def token(self):return json.loads(e.activation_bundle('LIC')['raw_json'])['enrollment_token']
 def envelope(self,token=None,instance='INST-ONE',key=None,**updates):
  key=key or self.key;p=dict(license_id='LIC',hardware_id='HW-ONE',instance_uuid=instance,request_epoch=int(time.time()),request_nonce=secrets.token_hex(24));p.update(updates)
  if token is not None:p['enrollment_token']=token
  return dict(payload=p,signature=base64.b64encode(key.sign(canonical(p))).decode(),public_key=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode())
 def post(self,body):
  with patch.object(h,'sign_data_payload',side_effect=MASTER.sign):return self.client.post('/api/v1/heartbeat/ping',json=body,base_url='https://localhost')
 def error(self,body,code):
  r=self.post(body);self.assertEqual(r.status_code,401);self.assertEqual(r.json['error'],code);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),0);return r
 def test_first_activation_then_normal_heartbeat_and_signed_reply(self):
  body=self.envelope(self.token());r=self.post(body);self.assertEqual(r.status_code,200);MASTER.public_key().verify(base64.b64decode(r.json['signature']),canonical(r.json['payload']));self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),1);self.assertEqual(len(self.sql('SELECT * FROM master_license_installations')),1);self.assertEqual(self.post(self.envelope()).status_code,200)
 def test_same_key_retry_does_not_duplicate(self):
  token=self.token();self.assertEqual(self.post(self.envelope(token)).status_code,200);self.assertEqual(self.post(self.envelope(token)).status_code,200);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),1);self.assertEqual(len(self.sql('SELECT * FROM master_license_installations')),1)
 def test_missing_token(self):self.error(self.envelope(),'enrollment_required')
 def test_invalid_token(self):self.error(self.envelope('x'*43),'enrollment_token_invalid')
 def test_expired_token(self):
  token=self.token();self.sql('UPDATE master_enrollment_tokens SET expires_epoch=0');self.error(self.envelope(token),'enrollment_token_expired')
 def test_different_license(self):self.error(self.envelope(self.token(),license_id='OTHER'),'enrollment_token_invalid')
 def test_wrong_hardware(self):
  token=self.token();self.sql("UPDATE master_licenses SET hardware_id='HW-BOUND'");self.error(self.envelope(token),'enrollment_binding_mismatch')
 def test_wrong_instance(self):
  token=self.token();self.sql("UPDATE master_licenses SET instance_id='INST-BOUND'");self.error(self.envelope(token),'enrollment_binding_mismatch')
 def test_revoked_after_issuance(self):
  token=self.token();self.sql("UPDATE master_licenses SET status='revoked'");self.error(self.envelope(token),'enrollment_license_unavailable')
 def test_expired_license_after_issuance(self):
  token=self.token();self.sql("UPDATE master_licenses SET expires_at='2020-01-01'");self.error(self.envelope(token),'enrollment_license_unavailable')
 def test_blacklist(self):
  token=self.token();self.sql("INSERT INTO master_blacklist VALUES('uuid','INST-ONE')");self.error(self.envelope(token),'enrollment_blocked')
 def test_disabled_instance(self):
  token=self.token();self.sql("INSERT INTO master_instances VALUES('INST-ONE','suspended')");self.error(self.envelope(token),'enrollment_blocked')
 def test_invalid_signature_does_not_consume_token(self):
  body=self.envelope(self.token());body['signature']=base64.b64encode(b'x'*64).decode();self.assertEqual(self.post(body).status_code,401);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),0);self.assertIsNone(self.sql('SELECT consumed_instance FROM master_enrollment_tokens')[0][0])
 def test_capacity_transaction_rejects_second_key(self):
  t1,t2=self.token(),self.token();self.assertEqual(self.post(self.envelope(t1)).status_code,200);r=self.post(self.envelope(t2,instance='INST-TWO'));self.assertEqual(r.json['error'],'enrollment_instance_limit');self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),1)
 def test_token_reuse_different_instance(self):
  token=self.token();self.assertEqual(self.post(self.envelope(token)).status_code,200);r=self.post(self.envelope(token,instance='INST-TWO'));self.assertEqual(r.json['error'],'enrollment_token_invalid')
 def test_never_replace_key_even_with_fresh_token(self):
  self.post(self.envelope(self.token()));r=self.post(self.envelope(self.token(),key=Ed25519PrivateKey.generate()));self.assertEqual(r.json['error'],'enrollment_key_mismatch')
 def test_nonce_replay(self):
  body=self.envelope(self.token());self.assertEqual(self.post(body).status_code,200);self.assertEqual(self.post(body).status_code,401);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),1)
 def test_invalid_epoch_creates_nothing(self):
  self.assertEqual(self.post(self.envelope(self.token(),request_epoch=0)).status_code,401);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),0)
 def test_rollback_if_nonce_insert_fails(self):
  body=self.envelope(self.token());self.sql('INSERT INTO master_request_nonces VALUES(?,?,?)',('INST-ONE',body['payload']['request_nonce'],int(time.time())));self.assertEqual(self.post(body).status_code,401);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),0);self.assertEqual(len(self.sql('SELECT * FROM master_license_installations')),0);self.assertIsNone(self.sql('SELECT consumed_instance FROM master_enrollment_tokens')[0][0])
 def test_two_simultaneous_instances_one_slot(self):
  bodies=[self.envelope(self.token(),instance='INST-TEST-'+str(i)) for i in range(2)]
  def send(body):
   with self.app.test_client() as client:return client.post('/api/v1/heartbeat/ping',json=body,base_url='https://localhost').status_code
  with patch.object(h,'sign_data_payload',side_effect=MASTER.sign),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(send,bodies))
  self.assertEqual(sorted(codes),[200,401]);self.assertEqual(len(self.sql('SELECT * FROM master_instance_keys')),1);self.assertEqual(len(self.sql('SELECT * FROM master_license_installations')),1)
 def test_bundle_does_not_modify_signed_license(self):
  before=self.sql('SELECT raw_json FROM master_licenses')[0][0];bundle=e.activation_bundle('LIC');self.assertIn('enrollment_token',json.loads(bundle['raw_json']));self.assertEqual(self.sql('SELECT raw_json FROM master_licenses')[0][0],before)
 def test_bound_license_reassignment_preserves_authorized_transfer(self):
  token=self.token();self.sql("UPDATE master_licenses SET instance_id='INST-ONE'");self.sql("INSERT INTO master_license_installations VALUES('LIC','INST-OLD')");self.assertEqual(self.post(self.envelope(token)).status_code,200);self.assertEqual(self.sql('SELECT instance_uuid FROM master_license_installations')[0][0],'INST-ONE')
 def test_actual_client_exchange_on_local_http_and_persistent_key(self):
  from werkzeug.serving import make_server
  client_path=Path(os.environ.get('MAX_CLIENT_PROTOCOL_TEST_PATH',str(ROOT.parent/'max_radius_local/core/license_protocol.py')));spec=importlib.util.spec_from_file_location('activation_client',client_path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  server=make_server('127.0.0.1',0,self.app);worker=threading.Thread(target=server.serve_forever);worker.start()
  try:
   with tempfile.TemporaryDirectory() as storage,patch.object(h,'sign_data_payload',side_effect=MASTER.sign):
    pem=MASTER.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode();payload={'license_id':'LIC','hardware_id':'HW-ONE','instance_uuid':'INST-ONE','enrollment_token':self.token()}
    result=module.exchange('http://127.0.0.1:'+str(server.server_port),payload,storage,pem);self.assertTrue(result['valid']);payload.pop('enrollment_token');self.assertTrue(module.exchange('http://127.0.0.1:'+str(server.server_port),payload,storage,pem)['valid'])
    payload['instance_uuid']='INST-NEW'
    with self.assertRaises(module.LicenseAuthenticationError) as caught:module.exchange('http://127.0.0.1:'+str(server.server_port),payload,storage,pem)
    self.assertEqual(caught.exception.code,'enrollment_required')
  finally:server.shutdown();worker.join();server.server_close()
 def test_issuer_routes_download_copy_auth_and_csrf(self):
  from core.license_security import install_csrf
  tree=ast.parse((ROOT/'app.py').read_text(encoding='utf8'));names={'login_required','license_activation_code','download_license_file'};ns=dict(app=self.app,wraps=wraps,session=session,request=request,redirect=redirect,url_for=url_for,flash=flash,jsonify=jsonify,send_file=send_file,io=io,activation_bundle=e.activation_bundle,EnrollmentError=e.EnrollmentError)
  def lookup(q,args):return {'license_id':'LIC'} if args==(1,) else None
  ns['query_one']=lookup;exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'issuer-routes','exec'),ns)
  self.app.secret_key='test-secret';self.app.add_url_rule('/login','login',lambda:'login');self.app.add_url_rule('/licenses','licenses_list',lambda:'licenses');install_csrf(self.app,lambda r:r.path!='/api/v1/heartbeat/ping')
  self.assertEqual(self.client.get('/licenses/1/download').status_code,302);self.assertEqual(len(self.sql('SELECT * FROM master_enrollment_tokens')),0)
  with self.client.session_transaction() as session_:session_['master_admin']='fixture';session_['_license_csrf']='test-csrf'
  self.assertEqual(self.client.post('/licenses/1/activation-code').status_code,403)
  response=self.client.post('/licenses/1/activation-code',headers={'X-CSRF-Token':'test-csrf'});self.assertEqual(response.status_code,200);self.assertEqual(response.headers['Cache-Control'],'no-store');bundle=json.loads(base64.b64decode(response.json['token_b64']));self.assertIn('enrollment_token',bundle)
  response=self.client.get('/licenses/1/download');self.assertEqual(response.status_code,200);self.assertEqual(response.headers['Cache-Control'],'no-store');self.assertIn('enrollment_token',response.json)
 def test_invalid_license_cannot_issue_capability(self):
  self.sql("UPDATE master_licenses SET status='suspended'")
  with self.assertRaises(e.EnrollmentError):self.token()
if __name__=='__main__':unittest.main()
