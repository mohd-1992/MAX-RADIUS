import unittest
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[2]/'deploy/hosted-max'))
import factory_fleet_proxy as p
class Tests(unittest.TestCase):
 def setUp(self):
  self.core='a'*64;self.web='b'*64;self.calls=[]
  self.rows=[{'Id':self.core,'Labels':{'com.docker.compose.project':'max','com.docker.compose.service':'radius_core'}},{'Id':self.web,'Labels':{'com.docker.compose.project':'max','com.docker.compose.service':'web'}}]
 def backend(self,path):return self.rows if path.startswith('/containers/json') else {'Config':{'Labels':self.rows[0]['Labels']}}
 def request(self,tenant='max',method='POST',path=None,allowed=True):
  return p.respond(tenant,method,path or '/containers/'+self.core+'/stop?t=10',self.backend,authorize=lambda *a:allowed,control=lambda *a:self.calls.append(a) or {})[0]
 def test_recorded_reset_stop_permitted(self):self.assertEqual(self.request(),200);self.assertEqual(self.calls,[(self.core,'stop')])
 def test_recorded_restore_start_permitted(self):self.assertEqual(self.request(path='/containers/'+self.core+'/start'),200)
 def test_no_reset_journal_rejected(self):self.assertEqual(self.request(allowed=False),403);self.assertFalse(self.calls)
 def test_other_tenant_rejected(self):self.assertEqual(self.request(tenant='other'),403)
 def test_other_core_rejected(self):self.assertEqual(self.request(path='/containers/'+'c'*64+'/stop'),404)
 def test_web_control_rejected(self):self.assertEqual(self.request(path='/containers/'+self.web+'/stop'),404)
 def test_shared_service_control_rejected(self):self.rows[0]['Labels']['com.docker.compose.service']='l2tp';self.assertEqual(self.request(),404)
 def test_restart_delete_exec_rejected(self):
  for action in ['restart','exec','kill','remove']:self.assertEqual(self.request(path='/containers/'+self.core+'/'+action),403)
 def test_unbounded_stop_timeout_rejected(self):self.assertEqual(self.request(path='/containers/'+self.core+'/stop?t=999'),403)
 def test_replaced_container_rejected(self):self.rows[0]['Labels']['com.docker.compose.project']='other';self.assertEqual(self.request(),404)
if __name__=='__main__':unittest.main(verbosity=2)
