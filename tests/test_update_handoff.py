"""Controller transfer ownership/recovery; no Docker socket, DB or network."""
import copy,json,tempfile,unittest
from pathlib import Path
from services import system_update_handoff as h

class API:
    def __init__(self,old,web):self.rows={old['Id']:copy.deepcopy(old),web['Id']:copy.deepcopy(web)};self.calls=[];self.lost=False
    def __call__(self,method,path,body=None):
        self.calls.append((method,path,copy.deepcopy(body)))
        if path=='/containers/json?all=1':return [dict(Id=r['Id'],Names=[r['Name']],Labels=r.get('Config',{}).get('Labels',{}),State='running' if r['State']['Running'] else 'exited') for r in self.rows.values()]
        if path.startswith('/containers/create?'):
            row=dict(Id='b'*64,Name='/'+path.split('name=')[1],Image=body['Image'],Config=copy.deepcopy(body),
                HostConfig=body['HostConfig'],Mounts=[],State=dict(Running=False,StartedAt='new'),NetworkSettings={'Networks':{}})
            self.rows[row['Id']]=row
            if self.lost:raise h.UpdateError('Lost response after creation')
            return {'Id':row['Id']}
        ident=path.split('/')[2];row=self.rows[ident]
        if method=='GET':return copy.deepcopy(row)
        if '/stop?' in path:row['State']['Running']=False
        elif path.endswith('/start'):row['State']['Running']=True
        elif '/update' in path:row['HostConfig']['RestartPolicy']=body['RestartPolicy']
        elif '/rename?' in path:row['Name']='/'+path.split('name=')[1]

class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        mounts=[dict(Type='bind',Source='/audit/'+str(i),Destination=d,RW=True) for i,d in enumerate(
            ('/max-update-private','/max-update-socket','/max-update-compose','/var/run/docker.sock'))]
        self.old=dict(Id='a'*64,Name='/owned_controller',Image='old',Config={'Labels':{'com.docker.compose.project':'owned','com.docker.compose.service':'update_controller'},'Env':['FIXTURE=1']},
            HostConfig={'RestartPolicy':{'Name':'unless-stopped'},'NetworkMode':'none'},Mounts=mounts,
            State={'Running':True,'StartedAt':'original'},NetworkSettings={'Networks':{}})
        self.web=dict(Id='c'*64,Name='/owned_web',Image='candidate',State={'Running':True})
        self.api=API(self.old,self.web);self.probes=[]
        self.compose=self.folder/'compose.json';self.compose.write_text(json.dumps({'services':{'web':{'image':'candidate'},'update_controller':{'image':'old'}}}))
        self.plan={'job':'d'*32,'images':{'web':'candidate'},'references':{'web':'mohd777/max-radius-web@sha256:'+'e'*64},'project':'owned','publisher_key':'fixture',
            'compose_path':str(self.compose),'controller':{'service':'update_controller','old':self.old}}
        h.write_json(self.folder/'plan.private.json',self.plan)
        h.write_json(self.folder/'state.json',{'status':'handoff_pending','services':{'web':{'new_id':self.web['Id']}}})
    def tearDown(self):self.temp.cleanup()
    def probe(self,*args,**kwargs):self.probes.append(kwargs.get('previous',False))
    def run_handoff(self,probe=None):return h.run(self.folder,self.api,probe or self.probe)
    def test_success_is_verified_and_persistent_and_not_replayed(self):
        result=self.run_handoff();self.assertEqual(result['status'],'completed');self.assertEqual(result['engine_protocol'],3)
        import yaml
        self.assertEqual(yaml.safe_load(self.compose.read_text())['services']['update_controller']['image'],self.plan['references']['web'])
        self.assertFalse(self.api.rows[self.old['Id']]['State']['Running']);self.assertEqual(self.probes,[False])
        n=len(self.api.calls);self.run_handoff();self.assertEqual(len(self.api.calls),n)
    def test_candidate_failure_restores_and_probes_previous_controller(self):
        def probe(*a,**kw):
            self.probes.append(kw.get('previous',False))
            if not kw.get('previous'):raise h.UpdateError('Bad controller')
        self.assertEqual(self.run_handoff(probe)['status'],'recovery_required')
        self.assertTrue(self.api.rows[self.old['Id']]['State']['Running']);self.assertEqual(self.probes,[False,True])
        self.assertEqual(json.loads(self.compose.read_text())['services']['update_controller']['image'],'old')
    def test_lost_create_does_not_create_twice(self):
        self.api.lost=True;self.assertEqual(self.run_handoff()['status'],'recovery_required')
        self.assertEqual(sum('/containers/create?' in p for _,p,_ in self.api.calls),1)
        self.assertTrue(self.api.rows[self.old['Id']]['State']['Running'])
    def test_interrupted_handoff_never_replays(self):
        h.write_json(self.folder/'controller-handoff.private.json',{'attempted':True})
        self.assertEqual(self.run_handoff()['status'],'recovery_required');self.assertEqual(self.api.calls,[])
    def test_changed_controller_aborts_before_stop(self):
        self.api.rows[self.old['Id']]['Config']['Env']=['changed']
        self.assertEqual(self.run_handoff()['status'],'recovery_required');self.assertFalse(any('/stop?' in p for _,p,_ in self.api.calls))
    def test_changed_web_aborts_before_stop(self):
        self.api.rows[self.web['Id']]['Image']='foreign'
        self.assertEqual(self.run_handoff()['status'],'recovery_required');self.assertFalse(any('/stop?' in p for _,p,_ in self.api.calls))
    def test_external_compose_change_is_not_overwritten(self):
        def probe(*a,**kw):
            if not kw.get('previous'):self.compose.write_text('foreign edit')
        self.assertEqual(self.run_handoff(probe)['status'],'recovery_required');self.assertEqual(self.compose.read_text(),'foreign edit')
    def test_core_bundle_does_not_transfer_controller(self):
        self.plan['images']['core']='other';h.write_json(self.folder/'plan.private.json',self.plan)
        self.assertEqual(self.run_handoff()['status'],'recovery_required');self.assertEqual(self.api.calls,[])
    def test_freeze_checks_exact_project_and_mount_sources(self):
        config={'project':'owned','controller_mounts':[dict(Target=m['Destination'],Source=m['Source']) for m in self.old['Mounts']]}
        value=h.freeze(self.api,config,{'services':{'update_controller':{}}});self.assertEqual(value['old']['Id'],self.old['Id'])
        config['controller_mounts'][0]['Source']='/foreign'
        with self.assertRaises(h.UpdateError):h.freeze(self.api,config,{'services':{'update_controller':{}}})
    def test_stopped_controller_does_not_become_current(self):
        config={'project':'owned','controller_mounts':[dict(Target=m['Destination'],Source=m['Source']) for m in self.old['Mounts']]}
        self.api.rows[self.old['Id']]['State']['Running']=False
        with self.assertRaises(h.UpdateError):h.freeze(self.api,config,{'services':{'update_controller':{}}})

    def test_unsupported_namespace_is_rejected_during_freeze_before_stop(self):
        self.api.rows[self.old['Id']]['HostConfig']['NetworkMode']='container:foreign'
        config={'project':'owned','controller_mounts':[
            dict(Target=m['Destination'],Source=m['Source']) for m in self.old['Mounts']]}
        with self.assertRaises(h.UpdateError):h.freeze(self.api,config,{'services':{'update_controller':{}}})
        self.assertFalse(any(method!='GET' for method,path,body in self.api.calls))

    def test_legacy_socket_is_recovered_only_from_owned_read_only_web_mount(self):
        web=self.api.rows[self.web['Id']]
        web['Config']={'Labels':{'com.docker.compose.project':'owned','com.docker.compose.service':'web'}}
        web['Mounts']=[dict(Type='bind',Source='/audit/1',Destination='/max-update-socket',RW=False)]
        config={'project':'owned','services':{'web':'web'},'controller_mounts':[
            dict(Target=m['Destination'],Source=m['Source']) for m in self.old['Mounts'] if m['Destination']!='/max-update-socket']}
        result=h.freeze(self.api,config,{'services':{'update_controller':{}}})
        self.assertEqual(result['legacy_socket_mount']['Source'],'/audit/1')
        web['Mounts'][0]['Source']='/foreign'
        with self.assertRaises(h.UpdateError):h.freeze(self.api,config,{'services':{'update_controller':{}}})

if __name__=='__main__':unittest.main()
