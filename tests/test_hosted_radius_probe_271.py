"""Only the new tenant-scoped RADIUS diagnostic capability is exercised here."""
import copy,importlib,importlib.util,json,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('probe_proxy_271',ROOT/'deploy/hosted-max/tenant_docker_proxy.py');proxy=importlib.util.module_from_spec(spec);spec.loader.exec_module(proxy)
contracts=importlib.import_module('tests.test_hosted_control_contract');h=contracts.h
class HostedProbeClientTests(unittest.TestCase):
 def setUp(self):
  contracts.ContractTests.setUp(self);self.data['capabilities']=['radius_probe_v1'];self.calls=[];self.reply=dict(schema=1,tenant_id=self.tenant,core_id='2'*64,exit_code=0,output='Received Access-Accept Id 1')
  def request(method,path,body=None):
   self.calls.append((method,path,body));return copy.deepcopy(self.data if method=='GET' else self.reply)
  h.request.side_effect=request
 def test_new_capability_pins_body_to_current_core(self):
  result=h.radius_simulation('local-user','local-pwd','local-secret');self.assertEqual(result['exit_code'],0);method,path,body=self.calls[-1];self.assertEqual((method,path),('POST','/max/v1/radius/probe'));self.assertEqual(body['expected_core_id'],'2'*64);self.assertEqual(set(body),{'username','password','secret','expected_core_id'})
 def test_old_proxy_without_capability_never_gets_probe_post(self):
  self.data.pop('capabilities')
  with self.assertRaises(RuntimeError):h.radius_simulation('user','pwd','secret')
  self.assertEqual([x[0] for x in self.calls],['GET'])
 def test_external_server_or_custom_port_never_crosses_socket(self):
  for server,port in [('198.51.100.10',1812),('127.0.0.1',1912)]:
   with self.assertRaises(RuntimeError):h.radius_simulation('user','pwd','secret',server,port)
  self.assertEqual(self.calls,[])
 def test_response_cannot_switch_tenant_or_core(self):
  for field,value in [('tenant_id','b'*32),('core_id','9'*64),('exit_code',True),('output','x'*65537)]:
   saved=self.reply[field];self.reply[field]=value
   with self.assertRaises(RuntimeError):h.radius_simulation('user','pwd','secret')
   self.reply[field]=saved
 def test_stopped_owned_core_is_not_started_or_probed(self):
  self.data['services']['core']['State']['Running']=False
  with self.assertRaises(RuntimeError):h.radius_simulation('user','pwd','secret')
  self.assertEqual([x[0] for x in self.calls],['GET'])
class GatewayProbeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.tenant='a'*32;self.binding=dict(id=self.tenant,slug='local',domain='local.max-r.cc',state='ready',instance='fixture',pool='local-probe',containers=dict(web='probe-web',core='probe-core'),images=dict(web='web-image',core='core-image'));p=Path(self.tmp.name)/'binding.json';p.write_text(json.dumps(self.binding));self.rows={}
  for n,role in enumerate(('web','core','db'),1):
   labels={'max.platform':'max-r','max.tenant':self.tenant,'max.service':role} if role!='db' else {'max.pool':'local-probe'};name='probe-'+role if role!='db' else 'max-db-local-probe';self.rows[role]=dict(Id=str(n)*64,Name='/'+name,Created='2026-11-08T00:00:00Z',Image='sha256:'+str(n)*64,Config=dict(Image=role+'-image',Labels=labels),State=dict(Running=True,Status='running',Paused=False,Restarting=False),RestartCount=0)
  self.api_calls=[];self.core_reads=0;self.replace_core=False
  def api(method,path):
   self.api_calls.append((method,path));self.assertEqual(method,'GET');ref=path.split('/')[2];row=next(v for v in self.rows.values() if ref in (v['Id'],v['Name'].lstrip('/')))
   if row is self.rows['core']:
    self.core_reads+=1
    if self.replace_core and self.core_reads>1:row=copy.deepcopy(row);row['Id']='4'*64
   return 200,json.dumps(row).encode()
  self.runner=Mock(return_value=dict(exit_code=0,output='Received Access-Accept Id 1'));self.scope=proxy.Scope(dict(binding=str(p),tenant_id=self.tenant,slug='local'),api=api,probe_runner=self.runner);self.payload=dict(username='local-user',password='local-pwd',secret='local-secret',expected_core_id='2'*64)
 def test_state_advertises_narrow_capability(self):
  status,raw=self.scope.handle('GET','/max/v1/state');self.assertEqual(status,200);self.assertIn('radius_probe_v1',json.loads(raw)['capabilities']);self.runner.assert_not_called()
 def test_probe_runs_only_current_full_core_id(self):
  status,raw=self.scope.handle('POST','/max/v1/radius/probe',self.payload);self.assertEqual(status,200);value=json.loads(raw);self.assertEqual(value['core_id'],'2'*64);self.assertEqual(value['tenant_id'],self.tenant);self.runner.assert_called_once_with('2'*64,self.payload);self.assertFalse(any(m!='GET' for m,p in self.api_calls))
 def test_old_or_foreign_expected_id_never_executes(self):
  self.payload['expected_core_id']='9'*64
  with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',self.payload)
  self.runner.assert_not_called()
 def test_replacement_between_inspection_and_effect_is_denied(self):
  self.replace_core=True
  with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',self.payload)
  self.runner.assert_not_called()
 def test_foreign_service_labels_are_denied(self):
  self.rows['core']['Config']['Labels']['max.tenant']='b'*32
  with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',self.payload)
  self.runner.assert_not_called()
 def test_stopped_paused_or_restarting_core_is_not_controlled(self):
  for key,value in [('Running',False),('Paused',True),('Restarting',True)]:
   old=self.rows['core']['State'][key];self.rows['core']['State'][key]=value
   with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',self.payload)
   self.rows['core']['State'][key]=old
  self.runner.assert_not_called()
 def test_parallel_probe_is_rejected_without_queue_or_exec(self):
  self.scope.probe_lock.acquire()
  try:
   with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',self.payload)
  finally:self.scope.probe_lock.release()
  self.runner.assert_not_called()
 def test_no_command_container_or_server_arguments_are_accepted(self):
  for key in ('command','container','server','port'):
   body=dict(self.payload);body[key]='foreign'
   with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',body)
  self.runner.assert_not_called()
 def test_credentials_are_bounded_and_single_line(self):
  for key,value in [('username','-x'),('password','x\ny'),('secret','x'*129),('username',''),('password',None)]:
   body=dict(self.payload);body[key]=value
   with self.assertRaises(proxy.Denied):self.scope.handle('POST','/max/v1/radius/probe',body)
  self.runner.assert_not_called()
 def test_bodies_cannot_be_added_to_other_control_routes(self):
  for method,path in [('GET','/containers/json'),('POST','/containers/'+'2'*64+'/restart')]:
   with self.assertRaises(proxy.Denied):self.scope.handle(method,path,self.payload)
  self.runner.assert_not_called();self.assertFalse(any(m!='GET' for m,p in self.api_calls))
 def test_query_or_encoded_probe_path_is_not_an_exec_alias(self):
  for path in ('/max/v1/radius/probe?core=foreign','/max/v1/radius/%70robe'):
   with self.assertRaises(proxy.Denied):self.scope.handle('POST',path,self.payload)
  self.runner.assert_not_called()
class FixedProbeRunnerTests(unittest.TestCase):
 def setUp(self):
  self.payload=dict(username='local-user',password='LOCAL-PASSWORD',secret='LOCAL-SECRET',expected_core_id='2'*64);self.file=tempfile.TemporaryFile();self.addCleanup(self.file.close)
 def run_output(self,data,timeout=False,running=False):
  self.file.write(data);self.file.seek(0);proc=types.SimpleNamespace(stdout=self.file,poll=Mock(return_value=None if running else 0),wait=Mock(return_value=0),kill=Mock());self.process=proc
  with patch.object(proxy.subprocess,'Popen',return_value=proc) as popen,patch.object(proxy.select,'select',return_value=([] if timeout else [self.file],[],[])):
   self.popen=popen;return proxy.run_radius_probe('2'*64,self.payload)
 def test_fixed_argv_loopback_and_credential_redaction(self):
  result=self.run_output(b'Received Access-Accept Id 1\nUser-Password = "LOCAL-PASSWORD"\nSession-Timeout = 60\n');cmd=self.popen.call_args.args[0];self.assertEqual(cmd,['docker','exec','2'*64,'radtest','local-user','LOCAL-PASSWORD','127.0.0.1','0','LOCAL-SECRET']);self.assertNotIn('shell',self.popen.call_args.kwargs);self.assertNotIn('LOCAL-PASSWORD',result['output']);self.assertEqual(result['exit_code'],0)
 def test_timeout_has_no_verified_response_and_only_kills_own_child(self):
  result=self.run_output(b'',timeout=True,running=True);self.assertEqual(result['exit_code'],-1);self.assertIn('timeout',result['output']);self.process.kill.assert_called_once()
 def test_output_limit_is_enforced(self):
  with self.assertRaises(proxy.Denied):self.run_output(b'x'*65537,running=True)
  self.process.kill.assert_called_once()
 def test_target_id_cannot_be_changed_even_for_direct_runner(self):
  with patch.object(proxy.subprocess,'Popen') as popen:
   with self.assertRaises(proxy.Denied):proxy.run_radius_probe('9'*64,self.payload)
   popen.assert_not_called()
if __name__=='__main__':unittest.main()
