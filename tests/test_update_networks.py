"""Built-in Docker networks differ from user-defined network endpoints."""
import copy
import unittest
from services import system_update_runtime as r


def container(mode, networks):
    return {'Id':'a'*64,'Image':'old','Name':'/owned','Config':{'Hostname':'oldhost','Env':[]},
            'HostConfig':{'NetworkMode':mode,'RestartPolicy':{'Name':'unless-stopped'}},
            'Mounts':[], 'NetworkSettings':{'Networks':networks}}


class NetworkContractTests(unittest.TestCase):
    def test_default_bridge_never_reuses_explicit_ip_or_aliases(self):
        for mode in ('bridge','default'):
            old=container(mode,{'bridge':{'IPAddress':'172.17.0.7','Aliases':['owned']}})
            endpoint=r.endpoints_for(old)
            self.assertEqual(endpoint,{'bridge':{}})
            self.assertEqual(r.candidate_spec(old,'new',endpoint)['NetworkingConfig'],
                             {'EndpointsConfig':{'bridge':{}}})

    def test_default_bridge_can_reconnect_a_stopped_endpoint(self):
        self.assertEqual(r.endpoints_for(container('bridge',{'bridge':{'IPAddress':''}})),{'bridge':{}})

    def test_custom_bridge_preserves_address_and_service_aliases(self):
        old=container('owned_net',{'owned_net':{'IPAddress':'10.12.0.8',
                    'Aliases':['oldhost','a'*64,'a'*12,'web','owned']}})
        self.assertEqual(r.endpoints_for(old),{'owned_net':{
            'IPAMConfig':{'IPv4Address':'10.12.0.8'},'Aliases':['web','owned']}})

    def test_builtin_none_and_host_have_no_endpoint_override(self):
        for mode in ('none','host'):
            for nets in ({},{mode:{'IPAddress':''}}):
                old=container(mode,nets);endpoints=r.endpoints_for(old)
                self.assertEqual(endpoints,{})
                self.assertNotIn('NetworkingConfig',r.candidate_spec(old,'new',endpoints))

    def test_unknown_or_shared_namespaces_are_rejected(self):
        for old in (container('container:foreign',{}),container('unknown',{}),
                    container('none',{'foreign':{}}),container('bridge',{'host':{}}),
                    container('owned',{'owned':{'IPAddress':''}})):
            with self.assertRaises(r.UpdateError):r.endpoints_for(old)

    def test_dynamic_bridge_recovery_uses_empty_endpoint_configuration(self):
        old=container('bridge',{'bridge':{'IPAddress':'172.17.0.7'}})
        live=copy.deepcopy(old);live['State']={'Running':False};live['NetworkSettings']['Networks']={}
        calls=[]
        def api(method,path,body=None):
            calls.append((method,path,body))
            if method=='GET' and path=='/containers/json?all=1':return [{'Id':old['Id'],'Names':['/owned']}]
            if method=='GET':return copy.deepcopy(live)
            if '/networks/bridge/connect'==path:live['NetworkSettings']['Networks']['bridge']={}
            if path.endswith('/start'):live['State']['Running']=True
        r.restore(api,{'old':old,'job':'job','target_id':'new','endpoints':r.endpoints_for(old)}, {})
        reconnect=next(body for method,path,body in calls if path=='/networks/bridge/connect')
        self.assertEqual(reconnect['EndpointConfig'],{})
        self.assertTrue(live['State']['Running'])
