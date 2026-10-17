"""Isolated clock recovery contracts; never import the application or connect to DB/NAS."""
import ast,base64,hashlib,importlib.util,json,logging,math,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('clock_protocol_fixture',ROOT/'core/license_protocol.py');protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
class ClockRecovery(unittest.TestCase):
 def fixture(self,reply=None,error=None,race=False):
  now=1800000000.;state={'value':str(now+10800)};writes=[]
  def write(s,p=()):
   writes.append((s,p))
   if "AND `value` = ?" in s and state['value']==p[1] and not race:state['value']=p[0]
  ns={'time':MagicMock(time=lambda:now,sleep=lambda _:None),'math':math,'json':json,'logger':logging.getLogger('clock-test'),'query_one':lambda _:dict(state),'execute_write':write,'clear_license_cache':lambda:None}
  tree=ast.parse((ROOT/'services/license_guard_service.py').read_text(encoding='utf-8-sig'))
  for name in ('_recover_clock_after_verified_heartbeat','check_and_update_monotonic_time','sync_license_heartbeat_with_server'):
   fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(compile(ast.Module(body=[fn],type_ignores=[]),'guard-fixture','exec'),ns)
  proof={'license_id':'fixture','valid':True,'status':'active','issued_epoch':now-1,'expires_epoch':now+60}
  if reply:proof.update(reply)
  return ns,state,writes,proof
 def test_verified_fresh_confirmation_recovers_only_guard(self):
  n,s,w,p=self.fixture();self.assertTrue(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(float(s['value']),1800000000);self.assertEqual(len(w),1);self.assertIn('AND `value` = ?',w[0][0])
 def test_unsigned_or_invalid_status_never_resets(self):
  for change in ({'license_id':'other'},{'valid':False},{'status':'revoked'},{'status':'expired'},{'status':'suspended'},{'remote_action':'revoke'},{'remote_action':'suspend'}):
   with self.subTest(change=change):
    n,s,w,p=self.fixture(change);self.assertFalse(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(w,[])
 def test_invalid_or_stale_time_never_resets(self):
  for change in ({'issued_epoch':None},{'issued_epoch':float('nan')},{'issued_epoch':float('inf')},{'issued_epoch':True},{'expires_epoch':float('nan')},{'issued_epoch':1800000031.},{'expires_epoch':1799999999.},{'expires_epoch':1800000300.},{'expires_epoch':1799999998.}):
   with self.subTest(change=change):
    n,s,w,p=self.fixture(change);self.assertFalse(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(w,[])
 def test_healthy_guard_does_not_reset(self):
  n,s,w,p=self.fixture();s['value']='1799999990';self.assertFalse(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(w,[])
 def test_corrupt_guard_stays_locked(self):
  for v in ('broken','nan','inf'):
   n,s,w,p=self.fixture();s['value']=v;self.assertFalse(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(w,[])
 def test_concurrent_update_wins(self):
  n,s,w,p=self.fixture(race=True);old=s['value'];self.assertFalse(n['_recover_clock_after_verified_heartbeat'](p,'fixture'));self.assertEqual(s['value'],old)
 def heartbeat(self,error=None,response=None):
  n,s,w,p=self.fixture(response);lic={'id':1,'license_id':'fixture','raw_package_json':'{"payload":{"limits":{}}}'}
  oldquery=n['query_one'];n.update(DEFAULT_MASTER_SERVER_URL='https://license.invalid',get_current_system_counts=lambda:(0,0,0),get_machine_id=lambda:'hw',get_instance_uuid=lambda:'instance',APP_VERSION='test',APP_EDITION='test',get_connection=MagicMock,is_mysql_conn=lambda _:True,STORAGE_DIR='fixture',get_master_public_key_pem=lambda:'fixture',remove_license_revocation=lambda _:None,record_license_revocation=lambda *a:None)
  n['query_one']=lambda q:lic if 'FROM wisp_license_info' in q else oldquery(q)
  def exchange(*a):
   if error:raise error
   return p
  n['authenticated_license_exchange']=exchange
  return n,s,w
 def test_successful_heartbeat_integrates_recovery(self):
  n,s,w=self.heartbeat();self.assertTrue(n['sync_license_heartbeat_with_server'](1)[0]);self.assertEqual(float(s['value']),1800000000.)
 def test_offline_does_not_unlock(self):
  n,s,w=self.heartbeat(TimeoutError());self.assertFalse(n['sync_license_heartbeat_with_server'](1)[0]);self.assertEqual(w,[]);self.assertFalse(n['check_and_update_monotonic_time']()[0])
 def test_revoked_and_expired_cannot_reset_guard(self):
  for status in ('revoked','expired','suspended'):
   n,s,w=self.heartbeat(response={'valid':False,'status':status});old=s['value'];self.assertFalse(n['sync_license_heartbeat_with_server'](1)[0]);self.assertEqual(s['value'],old)
class SignedTime(unittest.TestCase):
 def setUp(self):
  self.key=Ed25519PrivateKey.generate();self.pem=self.key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode();self.req={'license_id':'test','hardware_id':'hw','instance_uuid':'instance','request_nonce':'fresh'};self.response={**self.req,'request_hash':hashlib.sha256(protocol.canonical(self.req)).hexdigest(),'issued_epoch':time.time()-1,'expires_epoch':time.time()+60,'valid':True,'status':'active'}
 def envelope(self):return {'payload':self.response,'signature':base64.b64encode(self.key.sign(protocol.canonical(self.response))).decode()}
 def test_signed_epoch_survives_verification(self):self.assertEqual(protocol.verify_response(self.envelope(),self.req,self.pem)['issued_epoch'],self.response['issued_epoch'])
 def test_modified_time_signature_rejected(self):
  e=self.envelope();e['payload']['issued_epoch']+=1
  with self.assertRaises(Exception):protocol.verify_response(e,self.req,self.pem)
 def test_replay_wrong_request_rejected(self):
  for field in ('license_id','hardware_id','instance_uuid','request_nonce'):
   with self.subTest(field=field),self.assertRaises(ValueError):protocol.verify_response(self.envelope(),{**self.req,field:'other'},self.pem)
 def test_nonfinite_boolean_reversed_and_expired_intervals_rejected(self):
  for change in ({'issued_epoch':float('nan')},{'expires_epoch':float('inf')},{'issued_epoch':True},{'issued_epoch':time.time()+61},{'expires_epoch':time.time()-2}):
   old=self.response.copy();self.response.update(change)
   with self.subTest(change=change),self.assertRaises(ValueError):protocol.verify_response(self.envelope(),self.req,self.pem)
   self.response=old
if __name__=='__main__':unittest.main()
