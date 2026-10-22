"""Isolated target-binding regression; never imports app/DB/watchdog workers."""
import ast
import copy
import datetime
import os
from pathlib import Path
import re
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
ROLES=('autoheal','freeradius','wisp-web','mariadb','l2tp')
NAMES=('max_radius_autoheal','max_radius_core','max_radius_web','max_radius_db','max_radius_l2tp')

def load_functions():
    source=(ROOT/'services/autoheal_service.py').read_text(encoding='utf-8-sig')
    tree=ast.parse(source)
    names={'_runtime_container_ids','get_container_fleet_status','_get_fallback_container_fleet','_resolve_managed_container','_allowed_container','inspect_container_details','get_live_container_logs','restart_system_container'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    # This fixture exercises container ownership only. Entitlement decorators
    # have separate policy tests and must not load a real license/DB here.
    for node in nodes:
        node.decorator_list=[]
    ns={'os':types.SimpleNamespace(environ={'HOSTNAME':'f'*12}),'datetime':datetime,'re':re,'MONITORED_CONTAINER_NAMES':list(NAMES)}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'isolated-autoheal','exec'),ns)
    return ns

def item(i,name,role,project='fixture',state='running'):
    return dict(Id=('%064x'%i),Names=['/'+name],State=state,Status='Up (healthy)' if state=='running' else 'Exited (0)',Image='fixture-image',Created=i,
                Labels={'com.docker.compose.project':project,'com.docker.compose.service':role})

class Binding(unittest.TestCase):
    def setUp(self):
        self.ns=load_functions();self.items=[item(i+1,n,r) for i,(n,r) in enumerate(zip(NAMES,ROLES))]
        self.old=item(255,'max_web_previous_fixture','wisp-web',state='exited');self.items.insert(0,self.old)
        self.calls=[];self.events=[];self.inspect_hook=None
        self.ns['_runtime_container_ids']=lambda:set()
        self.ns['docker_api_request']=self.api
        self.ns['log_system_alert']=lambda *a,**kw:self.events.append((a,kw))
        self.ns['execute_update']=lambda *a,**kw:None
    def api(self,method,path,*a,**kw):
        self.calls.append((method,path))
        if path=='/containers/json?all=1':return copy.deepcopy(self.items),None
        if path=='/containers/'+'f'*12+'/json':return {'Config':{'Labels':self.old['Labels']}},None
        ident=path.split('/')[2]
        value=next((x for x in self.items if x['Id']==ident),None)
        if not value:return None,'missing'
        if path.endswith('/json'):
            value=copy.deepcopy(value)
            result={'Id':value['Id'],'Name':value['Names'][0],'Config':{'Labels':value['Labels']},'State':{'Running':value['State']=='running'}}
            if self.inspect_hook:self.inspect_hook(result)
            return result,None
        if '/logs?' in path:return 'fixture-only log',None
        if method=='POST' and '/restart?' in path:return True,None
        raise AssertionError('Unexpected Docker operation')
    def fleet(self):return self.ns['get_container_fleet_status']()
    def web(self):return next(x for x in self.fleet() if x['role']=='web')
    def no_mutation(self):self.assertFalse(any(method!='GET' for method,_ in self.calls))
    def test_inherited_hostname_never_selects_stopped_archive(self):
        self.assertEqual(self.web()['container_id'],'%064x'%3)
        self.assertTrue(self.web()['is_healthy'])
    def test_list_order_does_not_change_targets(self):
        self.items.reverse();self.assertEqual(self.web()['container_id'],'%064x'%3)
    def test_proven_runtime_identity_wins_over_foreign_hostname(self):
        self.ns['_runtime_container_ids']=lambda:{'%064x'%3}
        self.old['Labels']['com.docker.compose.project']='foreign'
        self.assertEqual(self.web()['compose_project'],'fixture')
    def test_foreign_newer_healthy_container_excluded(self):
        self.items.append(item(300,'foreign_web','wisp-web','foreign'))
        self.assertEqual(self.web()['container_id'],'%064x'%3)
    def test_ambiguous_noncanonical_role_is_unavailable(self):
        self.items[3]['Names']=['/custom_web_a']
        self.items.append(item(301,'custom_web_b','wisp-web'))
        self.assertEqual(self.web()['id'],'N/A');self.no_mutation()
    def test_canonical_stopped_service_is_not_masked_by_archive(self):
        self.items[3]['State']='exited';self.old['State']='running'
        self.assertEqual(self.web()['container_id'],'%064x'%3)
        self.assertEqual(self.web()['state'],'exited')
    def test_alias_service_role_supported(self):
        self.items[3]['Labels']['com.docker.compose.service']='web'
        self.assertEqual(self.web()['container_id'],'%064x'%3)
    def test_all_five_buttons_use_full_ids(self):
        for target in self.fleet():
            ident=target['container_id'];self.assertEqual(len(ident),64)
            self.assertTrue(self.ns['restart_system_container'](ident)[0])
            self.assertIn(('POST','/containers/'+ident+'/restart?t=5'),self.calls)
    def test_archived_id_denied(self):
        self.assertFalse(self.ns['restart_system_container'](self.old['Id'])[0]);self.no_mutation()
    def test_archived_name_denied(self):
        self.assertFalse(self.ns['restart_system_container']('max_web_previous_fixture')[0]);self.no_mutation()
    def test_restart_requires_full_id(self):
        for bad in ['max_radius_web','%012x'%3,'../containers/web','',None]:
            self.assertFalse(self.ns['restart_system_container'](bad)[0])
        self.no_mutation()
    def test_foreign_id_denied(self):
        self.items.append(item(302,'foreign_web','wisp-web','foreign'))
        self.assertFalse(self.ns['restart_system_container']('%064x'%302)[0]);self.no_mutation()
    def test_renamed_target_denied_before_post(self):
        self.inspect_hook=lambda d:d.update(Name='/retained_after_refresh')
        self.assertFalse(self.ns['restart_system_container']('%064x'%3)[0]);self.no_mutation()
    def test_project_changed_during_check_denied(self):
        self.inspect_hook=lambda d:d['Config']['Labels'].update({'com.docker.compose.project':'foreign'})
        self.assertFalse(self.ns['restart_system_container']('%064x'%3)[0]);self.no_mutation()
    def test_role_changed_during_check_denied(self):
        self.inspect_hook=lambda d:d['Config']['Labels'].update({'com.docker.compose.service':'mariadb'})
        self.assertFalse(self.ns['restart_system_container']('%064x'%3)[0]);self.no_mutation()
    def test_logs_pin_full_id_even_for_canonical_name(self):
        self.assertEqual(self.ns['get_live_container_logs']('max_radius_web'),'fixture-only log')
        self.assertIn(('GET','/containers/'+'%064x'%3+'/logs?stdout=1&stderr=1&tail=60&timestamps=1'),self.calls)
        self.no_mutation()
    def test_stale_logs_do_not_fall_back_to_replacement(self):
        self.assertTrue(self.ns['get_live_container_logs'](self.old['Id']).startswith('تعذر استخراج السجلات:'));self.no_mutation()
    def test_inspect_pins_live_identity(self):
        result,error=self.ns['inspect_container_details']('%064x'%3)
        self.assertIsNone(error);self.assertEqual(result['Id'],'%064x'%3);self.no_mutation()
    def test_missing_project_fails_closed(self):
        for row in self.items:row['Labels']={}
        self.assertFalse(self.ns['restart_system_container']('%064x'%3)[0]);self.no_mutation()
    def test_docker_unavailable_has_no_actionable_id(self):
        self.ns['docker_api_request']=lambda *a,**kw:(None,'unavailable')
        self.assertFalse(self.ns['restart_system_container']('%064x'%3)[0]);self.no_mutation()

if __name__=='__main__':unittest.main()
