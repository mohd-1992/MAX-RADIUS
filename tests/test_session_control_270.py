"""Durable control contracts with no app, SQL, NAS, or background workers."""
import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from services import session_control_service as control


class SessionControl(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'STORAGE_DIR':self.temp.name,'DB_HOST':'fixture'});self.env.start();self.addCleanup(self.env.stop)
        self.schema='tenant_one';self.username='fixture';self.allowed=None;self.calls=[]
        self.rows=[dict(radacctid=1,acctuniqueid='unique-one',username='fixture',nasipaddress='192.0.2.1',
            acctsessionid='session-one',framedipaddress='198.51.100.1',callingstationid='02:00:00:00:00:01',
            acctstarttime=dt.datetime(2026,1,1),acctstoptime=None)]
        self.guard_calls=0;self.account_exists=True;self.receipt=True
        @contextlib.contextmanager
        def guard(username):
            self.guard_calls+=1
            yield
        @contextlib.contextmanager
        def job_lock(name): yield True
        db=types.ModuleType('database.db');db.query_one=self.one;db.query_all=self.all
        db.is_import_maintenance_active=lambda:False
        coa=types.ModuleType('services.coa_queue_service');coa._disconnect_account_guard=guard;coa._disconnect_single_session=self.disconnect
        lifecycle=types.ModuleType('services.account_lifecycle_service');lifecycle.job_lock=job_lock
        mac=types.ModuleType('services.account_mac_service');mac.get_mac_policy=lambda *a:self.allowed
        self.patch=patch.dict(sys.modules,{'database.db':db,'services.coa_queue_service':coa,
            'services.account_lifecycle_service':lifecycle,'services.account_mac_service':mac})
        self.patch.start();self.addCleanup(self.patch.stop)
        worker=patch.object(control,'start_worker',lambda:None);worker.start();self.addCleanup(worker.stop)

    def all(self,sql,args=()):
        return copy.deepcopy([r for r in self.rows if r['username']==args[0] and r['acctstoptime'] is None])

    def one(self,sql,args=()):
        if 'DATABASE()' in sql:return {'tenant_schema':self.schema}
        if 'mac_receipts' in sql:return {'operation_id':args[0]} if self.receipt else None
        if 'FROM wisp_' in sql:return {'username':self.username} if self.account_exists else None
        return next((copy.deepcopy(r) for r in self.rows if str(r['radacctid'])==str(args[0]) and r['username']==args[1]),None)

    def disconnect(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        return dict(success=True,status='nas_confirmed_disconnected',accounting_final=False)

    def submit(self,**kwargs):
        return control.submit_manual('voucher',1,'fixture',request_id='a'*32,**kwargs)

    def test_admission_is_pending_not_confirmed(self):
        result=self.submit();self.assertTrue(result['pending']);self.assertEqual(result['confirmed'],0)
        self.assertEqual(self.calls,[])

    def test_result_is_durable_across_processes(self):
        result=self.submit()
        code='from services.session_control_service import find,public;import json;print(json.dumps(public(find("'+result['job_id']+'"))))'
        value=json.loads(subprocess.check_output([sys.executable,'-B','-c',code],text=True))
        self.assertEqual(value['job_id'],result['job_id']);self.assertTrue(value['pending'])

    def test_same_request_does_not_repeat(self):
        a=self.submit();b=self.submit();self.assertEqual(a['job_id'],b['job_id'])
        control.process(a['job_id']);control.process(b['job_id'])
        self.assertEqual(len(self.calls),1)

    def test_all_sessions_and_nas_are_in_fixed_snapshot(self):
        self.rows.append(dict(self.rows[0],radacctid=2,acctuniqueid='two',nasipaddress='192.0.2.2',acctsessionid='two'))
        result=self.submit()
        self.rows.append(dict(self.rows[0],radacctid=3,acctuniqueid='new',acctsessionid='new'))
        before=copy.deepcopy(self.rows);control.process(result['job_id'])
        done=control.public(control.find(result['job_id']))
        self.assertEqual(done['confirmed'],2);self.assertEqual(len(self.calls),2);self.assertEqual(before,self.rows)
        self.assertFalse(done['accounting_final'])

    def test_changed_generation_is_not_disconnected(self):
        result=self.submit();self.rows[0]['acctuniqueid']='replacement'
        control.process(result['job_id']);self.assertEqual(self.calls,[])
        self.assertEqual(control.public(control.find(result['job_id']))['unknown'],1)

    def test_unknown_is_not_zero_or_success(self):
        result=self.submit();self.rows=[];control.process(result['job_id'])
        done=control.public(control.find(result['job_id']))
        self.assertEqual(done['state'],'failed');self.assertEqual(done['unknown'],1)

    def test_restart_after_uncertain_attempt_verifies_without_resend(self):
        result=self.submit();job=control.find(result['job_id']);job['attempted']=['1']
        control.write(control.root()/('pending-'+job['id']+'.json'),job)
        control.process(job['id']);self.assertTrue(self.calls[0][1]['verify_only'])

    def test_restore_invalidates_old_control_journal(self):
        result=self.submit();control.invalidate();control.process(result['job_id'])
        self.assertEqual(self.calls,[]);self.assertEqual(control.find(result['job_id'])['state'],'cancelled')

    def test_schema_change_cannot_target_other_tenant(self):
        result=self.submit();self.schema='tenant_two';control.process(result['job_id'])
        self.assertEqual(self.calls,[]);self.assertEqual(control.find(result['job_id'])['state'],'cancelled')

    def test_account_removed_does_not_target_replacement(self):
        result=self.submit();self.account_exists=False;control.process(result['job_id']);self.assertEqual(self.calls,[])

    def test_mac_preserves_allowed_and_disconnects_only_violation(self):
        self.rows.append(dict(self.rows[0],radacctid=2,acctuniqueid='two',callingstationid='02:00:00:00:00:02'))
        self.allowed=dict(enabled=True,addresses=[dict(mac='02:00:00:00:00:01',note='')])
        result=control.prepare('voucher',1,'fixture','admin','a'*32,control.policy_key(self.allowed))
        control.process(result['job_id']);done=control.public(control.find(result['job_id']))
        self.assertEqual(done['confirmed'],1);self.assertEqual(done['preserved'],1)
        self.assertEqual(self.calls[0][0][5],2)

    def test_mac_unknown_is_not_detached_or_allowed(self):
        self.rows[0]['callingstationid']=''
        self.allowed=dict(enabled=True,addresses=[dict(mac='02:00:00:00:00:01')])
        result=control.prepare('voucher',1,'fixture','admin','a'*32,control.policy_key(self.allowed))
        control.process(result['job_id']);done=control.public(control.find(result['job_id']))
        self.assertEqual(self.calls,[]);self.assertEqual(done['unknown'],1)

    def test_mac_no_router_action_before_policy_commit(self):
        self.allowed=dict(enabled=True,addresses=[]);self.receipt=False
        result=control.prepare('voucher',1,'fixture','admin','a'*32,control.policy_key(self.allowed))
        control.process(result['job_id']);self.assertEqual(self.calls,[]);self.assertTrue(control.public(control.find(result['job_id']))['pending'])

    def test_superseded_mac_policy_cancels_old_application(self):
        self.allowed=dict(enabled=True,addresses=[dict(mac='02:00:00:00:00:01')])
        result=control.prepare('voucher',1,'fixture','admin','a'*32,control.policy_key(self.allowed))
        self.allowed['enabled']=False;control.process(result['job_id'])
        self.assertEqual(self.calls,[]);self.assertEqual(control.find(result['job_id'])['state'],'cancelled')

    def test_partial_result_never_claims_full_success(self):
        self.rows.append(dict(self.rows[0],radacctid=2,acctuniqueid='two'))
        result=self.submit()
        def disconnect(*args,**kw):return dict(success=args[5]==1,accounting_final=False)
        sys.modules['services.coa_queue_service']._disconnect_single_session=disconnect
        control.process(result['job_id']);done=control.public(control.find(result['job_id']))
        self.assertEqual((done['state'],done['confirmed'],done['unknown']),('partial',1,1))

    def test_invalid_identifier_and_oversized_targets_refused(self):
        with self.assertRaises(ValueError):control.find('../secret')
        self.rows=self.rows*1001
        with self.assertRaises(ValueError):self.submit()

    def test_no_sessions_does_not_probe_default_router(self):
        self.rows=[];result=self.submit();control.process(result['job_id'])
        self.assertEqual(self.calls,[]);self.assertEqual(control.find(result['job_id'])['state'],'no_targets')

    def test_backup_ghost_does_not_contact_router(self):
        result=self.submit();self.rows[0]['acctterminatecause']='Backup-Restored-Closed'
        control.process(result['job_id']);self.assertEqual(self.calls,[])
        self.assertEqual(control.public(control.find(result['job_id']))['unknown'],1)

    def test_allowed_only_does_not_claim_accounting_final(self):
        self.allowed=dict(enabled=True,addresses=[dict(mac='02:00:00:00:00:01')])
        result=control.prepare('voucher',1,'fixture','admin','a'*32,control.policy_key(self.allowed))
        control.process(result['job_id']);done=control.public(control.find(result['job_id']))
        self.assertEqual(done['state'],'allowed_only');self.assertFalse(done['accounting_final']);self.assertEqual(self.calls,[])

    def test_subscriber_uses_same_fixed_snapshot_control(self):
        result=control.submit_manual('subscriber',1,'fixture',request_id='b'*32)
        control.process(result['job_id']);self.assertEqual(len(self.calls),1)
        self.assertEqual(control.find(result['job_id'])['kind'],'subscriber')

    def test_status_accepts_existing_disconnect_permission_with_ownership(self):
        import ast
        result=control.submit_manual('subscriber',1,'fixture',request_id='b'*32)
        module=ast.parse(Path('web/routes/system.py').read_text(encoding='utf8'))
        fn=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='api_session_operation');fn.decorator_list=[]
        class Response(dict):
            def __init__(self,**kw):super().__init__(**kw);self.headers={}
        owner=types.ModuleType('core.ownership');owner.scoped_query_one=lambda *a:{'id':1}
        ns=dict(get_current_manager=lambda:{'username':'fixture-admin'},has_permission=lambda p,m:p=='subscribers.disconnect',jsonify=Response,os=os)
        with patch.dict(sys.modules,{'core.ownership':owner}):
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'status-route','exec'),ns)
            response=ns['api_session_operation'](result['job_id'])
        self.assertTrue(response['success']);self.assertEqual(response.headers['Cache-Control'],'no-store, private')

    def test_status_passes_bound_blueprint_guard_but_unknown_actions_do_not(self):
        import ast
        from flask import Flask, request, jsonify
        tree=ast.parse(Path('web/routes/system.py').read_text(encoding='utf8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='guard_account_action_permissions')
        fn.decorator_list=[]
        ns=dict(request=request,jsonify=jsonify,get_current_manager=lambda:{'id':1},has_permission=lambda *a:True)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'status-blueprint-guard','exec'),ns)
        app=Flask('status-guard-acceptance')
        app.before_request(ns[fn.name])
        app.add_url_rule('/api/actions/session-operation/<job_id>','api_session_operation',lambda job_id:jsonify(delegated=True))
        app.add_url_rule('/api/actions/unregistered','unknown_action',lambda:jsonify(unsafe=True))
        client=app.test_client()
        self.assertEqual(client.get('/api/actions/session-operation/'+'a'*32).status_code,200)
        self.assertEqual(client.get('/api/actions/unregistered').status_code,403)

    def test_card_mac_routes_and_controls_use_the_declared_detail_permission(self):
        from core.permission_catalog import permission_rows
        declared={row['code'] for row in permission_rows()}
        self.assertIn('detail.vouchers.card_edit',declared)
        self.assertNotIn('vouchers.card_edit',declared)
        for path in ('web/mac_policy_routes.py','web/endpoint_permissions.py','web/routes/system.py',
                     'web/routes/vouchers.py','web/templates/active_card_users.html',
                     'web/templates/active_card_user_details.html','web/templates/batch_details.html'):
            with self.subTest(path=path):
                text=Path(path).read_text(encoding='utf8')
                self.assertIn("'detail.vouchers.card_edit'",text)
                self.assertNotIn("'vouchers.card_edit'",text)

    def test_status_rechecks_identity_ownership_and_permission(self):
        import ast
        result=self.submit()
        tree=ast.parse(Path('web/routes/system.py').read_text(encoding='utf8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='api_session_operation');fn.decorator_list=[]
        owner=types.ModuleType('core.ownership');owner.scoped_query_one=lambda *a:{'id':1}
        ns=dict(get_current_manager=lambda:{'id':1},has_permission=lambda *a:True,jsonify=lambda **kw:kw,os=os)
        with patch.dict(sys.modules,{'core.ownership':owner}):
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'status-route','exec'),ns)
            ns['get_current_manager']=lambda:None
            self.assertEqual(ns[fn.name](result['job_id'])[1],401)
            ns['get_current_manager']=lambda:{'id':1};owner.scoped_query_one=lambda *a:None
            self.assertEqual(ns[fn.name](result['job_id'])[1],404)
            owner.scoped_query_one=lambda *a:{'id':1};ns['has_permission']=lambda *a:False
            self.assertEqual(ns[fn.name](result['job_id'])[1],403)
            ns['has_permission']=lambda *a:True;self.schema='foreign'
            self.assertEqual(ns[fn.name](result['job_id'])[1],404)

    def test_subscriber_route_uses_canonical_username_and_handles_admission_failure(self):
        import ast
        module=ast.parse(Path('web/routes/subscribers.py').read_text(encoding='utf8'))
        fn=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='api_disconnect_subscriber');fn.decorator_list=[]
        request=types.SimpleNamespace(get_json=lambda **kw:{'username':'FIXTURE','operation_id':'b'*32})
        ns=dict(request=request,query_one=lambda *a:{'id':1,'username':'fixture'},jsonify=lambda **kw:kw,get_current_manager=lambda:{'username':'admin'})
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'subscriber-route','exec'),ns)
        with patch.object(control,'submit_manual',return_value={'pending':True}) as submit:
            self.assertTrue(ns['api_disconnect_subscriber']()['pending']);self.assertEqual(submit.call_args.args[2],'fixture')
        with patch.object(control,'submit_manual',side_effect=control.ControlBusy('fixture')):
            response,status=ns['api_disconnect_subscriber']();self.assertFalse(response['success']);self.assertEqual(status,409)
