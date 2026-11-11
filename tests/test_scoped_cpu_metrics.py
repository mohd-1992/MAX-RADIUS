import ast,copy,http.client,importlib.util,json,math,os,socket,sys,time,types,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy/hosted-max'))
import tenant_resource_proxy as proxy

class ProxyTests(unittest.TestCase):
 def setUp(self):
  proxy._CACHE.clear();self.calls=[];self.items=[];self.details={};self.stats={}
  for i,role in enumerate(['web','radius_core','db'],1):
   ident=str(i)*64;labels={'com.docker.compose.project':'max1','com.docker.compose.service':role}
   self.items.append({'Id':ident,'Labels':labels});self.details[ident]={'Id':ident,'Config':{'Labels':labels},'State':{'Running':True}}
   self.stats[ident]={'cpu_stats':{'system_cpu_usage':2000,'cpu_usage':{'total_usage':200}},'precpu_stats':{'system_cpu_usage':1000,'cpu_usage':{'total_usage':100}}}
 def backend(self,path):
  self.calls.append(path)
  if path.startswith('/containers/json?'):return self.items
  ident=path.split('/')[2]
  if path.endswith('/json'):return self.details[ident]
  if path.endswith('/stats?stream=false'):return self.stats[ident]
  raise AssertionError('Unexpected request')
 def test_only_owned_roles_aggregate_and_response_has_no_raw_docker_data(self):
  self.items.append({'Id':'f'*64,'Labels':{'com.docker.compose.project':'other','com.docker.compose.service':'web'}})
  value=proxy.collect('max1',self.backend);self.assertEqual(value['cpu_percent'],30)
  self.assertEqual(set(value['services']),proxy.ROLES);self.assertFalse(any('f'*64 in p for p in self.calls))
  self.assertNotIn('Config',json.dumps(value));self.assertNotIn('cpu_usage',json.dumps(value))
 def test_real_zero_is_zero(self):
  for v in self.stats.values():v['cpu_stats']['cpu_usage']['total_usage']=100
  self.assertEqual(proxy.collect('max1',self.backend)['cpu_percent'],0)
 def test_other_tenant_post_and_query_are_denied_without_docker_access(self):
  for tenant,method,path in [('other','GET','/_max_metrics/v1/resources'),('max1','POST','/_max_metrics/v1/resources'),('max1','GET','/_max_metrics/v1/resources?tenant=other')]:
   self.assertEqual(proxy.respond(tenant,method,path,self.backend)[0],403)
  self.assertEqual(self.calls,[])
 def test_missing_duplicate_foreign_or_stopped_role_fails_closed(self):
  for kind in ['missing','duplicate','foreign','stopped']:
   self.setUp()
   if kind=='missing':self.items.pop()
   if kind=='duplicate':self.items.append(copy.deepcopy(self.items[0]))
   if kind=='foreign':self.details['1'*64]['Config']['Labels']['com.docker.compose.project']='foreign'
   if kind=='stopped':self.details['1'*64]['State']['Running']=False
   status,value=proxy.respond('max1','GET','/_max_metrics/v1/resources',self.backend)
   self.assertEqual(status,503);self.assertIsNone(value['cpu_percent'])
 def test_missing_previous_sample_and_negative_counters_are_unavailable(self):
  for kind in ['system','used']:
   self.setUp();v=self.stats['1'*64]
   if kind=='system':v['precpu_stats']['system_cpu_usage']=0
   else:v['cpu_stats']['cpu_usage']['total_usage']=0
   self.assertEqual(proxy.respond('max1','GET','/_max_metrics/v1/resources',self.backend)[0],503)
 def test_short_cache_prevents_duplicate_docker_sampling(self):
  first=proxy.respond('max1','GET','/_max_metrics/v1/resources',self.backend);n=len(self.calls)
  self.assertEqual(first,proxy.respond('max1','GET','/_max_metrics/v1/resources',self.backend));self.assertEqual(len(self.calls),n)
 def test_container_ownership_rechecked_after_stats(self):
  def backend(path):
   v=self.backend(path)
   if path.startswith('/containers/1') and '/stats?' in path:self.details['1'*64]['State']['Running']=False
   return v
  self.assertEqual(proxy.respond('max1','GET','/_max_metrics/v1/resources',backend)[0],503)

class ReaderTests(unittest.TestCase):
 def setUp(self):
  tree=ast.parse((ROOT/'services/stats_service.py').read_text(encoding='utf-8'))
  nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ['UnixHTTPConnection','_fetch_scoped_cpu']]
  self.ns={'http':types.SimpleNamespace(client=http.client),'socket':socket,'json':json,'math':math,'time':time}
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'<scoped-reader>','exec'),self.ns)
  self.value={'schema':1,'tenant':'max1','cpu_available':True,'cpu_basis':'host_capacity','sampled_at':time.time(),'cpu_percent':0}
 def read(self,status=200):
  value=self.value
  class Connection:
   def __init__(self,*a):pass
   def request(self,*a):pass
   def getresponse(self):return types.SimpleNamespace(status=status,read=lambda n:json.dumps(value).encode())
   def close(self):pass
  self.ns['UnixHTTPConnection']=Connection
  return self.ns['_fetch_scoped_cpu']('/var/run/docker.sock','max1')
 def test_zero_and_variable_values_are_not_floored(self):
  for cpu in [0,.1,2.6,100]:
   self.value['cpu_percent']=cpu;self.assertEqual(self.read()['cpu_percent'],cpu)
 def test_wrong_scope_stale_null_and_invalid_numbers_do_not_use_fake_fallback(self):
  for key,value in [('tenant','other'),('sampled_at',0),('cpu_percent',None),('cpu_percent',float('nan')),('cpu_percent',-1),('cpu_percent',True),('cpu_percent',101),('cpu_basis','unknown')]:
   with self.subTest(key=key,value=value):
    before=self.value[key];self.value[key]=value;self.assertEqual(self.read(),{'cpu_available':False,'cpu_percent':0.5});self.value[key]=before
  self.assertFalse(self.read(403)['cpu_available'])

if __name__=='__main__':unittest.main()
