"""Subscriber admin regressions: actual handlers/SQL/templates, no app workers or live data."""
import datetime
import importlib.util
import json
import sys
import types
import unittest
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch
from flask import request, session
from test_package_page import fixture as package_fixture, load_functions, ROOT

def fixture():
    app, db, ns = package_fixture()
    db.executescript('DROP TABLE wisp_subscribers; DROP TABLE wisp_vouchers;')
    fields='username password full_name phone email national_id address service_type mac_binding static_ip status expires_at notes last_renewed_at pause_reason'
    db.execute('CREATE TABLE wisp_subscribers(id INTEGER PRIMARY KEY,package_id INTEGER,global_seq_id INTEGER,balance REAL,extra_quota_mb REAL,snap_volume_quota_mb REAL,'+','.join(x+' TEXT' for x in fields.split())+')')
    db.execute('CREATE TABLE wisp_vouchers(id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,balance REAL,package_id INTEGER)')
    db.execute('CREATE TABLE radcheck(username TEXT,attribute TEXT,op TEXT,value TEXT)')
    db.execute('CREATE TABLE radreply(username TEXT,attribute TEXT,op TEXT,value TEXT)')
    db.execute('ALTER TABLE radusergroup ADD COLUMN username TEXT')
    db.execute('ALTER TABLE radusergroup ADD COLUMN priority INTEGER')
    db.execute('CREATE TABLE wisp_audit_logs(id INTEGER PRIMARY KEY,admin_id INTEGER,username TEXT,action TEXT,module TEXT,details TEXT)')
    db.execute('CREATE TABLE user_audit_logs(user_type TEXT,user_id INTEGER,username TEXT,admin_name TEXT,action TEXT,change_details TEXT)')
    db.execute('CREATE TABLE wisp_invoices(id INTEGER PRIMARY KEY,invoice_number TEXT UNIQUE,subscriber_id INTEGER,subscriber_name TEXT,amount REAL,status TEXT,package_name TEXT,notes TEXT,paid_at TEXT)')
    db.execute('CREATE TABLE radacct(radacctid INTEGER,username TEXT,acctsessionid TEXT,nasipaddress TEXT,framedipaddress TEXT,acctstarttime TEXT,acctupdatetime TEXT,acctstoptime TEXT,callingstationid TEXT,acctinputoctets INTEGER,acctoutputoctets INTEGER,acctsessiontime INTEGER)')
    db.execute('CREATE TABLE nas(id INTEGER,shortname TEXT,nasname TEXT,type TEXT)')
    ns.update(datetime=datetime, json=json, Decimal=Decimal,InvalidOperation=InvalidOperation,ROUND_HALF_UP=ROUND_HALF_UP,
        is_mysql_conn=lambda conn:False,get_db_storage_now=lambda:datetime.datetime(2026,10,11,18),
        get_current_manager=lambda:session.get('manager'),session=session,
        format_bytes=lambda x:f'{x or 0} B',format_duration=str,
        get_user_audit_logs=lambda *a:[],get_user_usage_analytics=lambda *a:dict(has_data=False,labels=[]),
        get_subscriber_sessions=lambda *a,**kw:[],generate_invoice_number=lambda ident:'INV-'+str(ns['invoice_seq'].append(1) or len(ns['invoice_seq'])))
    ns['invoice_seq']=[]
    load_functions('database/db.py',['execute_in_transaction'],ns)
    load_functions('core/radius_sync.py',['sync_subscriber_to_radius'],ns)
    load_functions('services/subscriber_service.py',['create_subscriber','update_subscriber'],ns)
    load_functions('services/quick_action_service.py',['action_bulk_execute','action_add_wallet_balance','action_deduct_wallet_balance','_change_wallet_balance'],ns)
    ns['get_target_entity']=lambda kind,ident:(dict(db.execute('SELECT * FROM '+('wisp_subscribers' if kind=='subscriber' else 'wisp_vouchers')+' WHERE id=?',(ident,)).fetchone()),kind)
    routes=['guard_subscriber_permissions','subscriber_create_page','add_subscriber_action','edit_subscriber_action','subscriber_details','subscriber_sessions']
    load_functions('web/routes/subscribers.py',routes,ns)
    actions=['_get_target_params','guard_account_action_permissions','api_delete_entity','api_extend_time','api_terminate','api_renew','api_change_package','api_add_quota','api_usage_history','api_add_wallet','api_deduct_wallet','api_disconnect']
    load_functions('web/routes/system.py',actions,ns)
    for action in ['action_delete_entity','action_extend_time','action_terminate_subscription','action_renew_package','action_change_package','action_add_quota','action_disconnect_user']:
        ns[action]=lambda *a,**kw:(ns['action_calls'].append((a,kw)) or True,'ok')
    ns['action_calls']=[]
    ns['action_get_usage_history']=lambda *a:(True,'ok',[])
    for path,endpoint,methods in [('/subscribers/create','subscriber_create_page',['GET','POST']),('/subscribers/add','add_subscriber_action',['POST']),
        ('/subscribers/edit/<int:sub_id>','edit_subscriber_action',['POST']),('/subscribers/<int:sub_id>','subscriber_details',['GET']),('/subscribers/sessions/<username>','subscriber_sessions',['GET'])]:
        app.add_url_rule(path,endpoint,ns[endpoint],methods=methods)
    for action,ep in [('delete','api_delete_entity'),('extend-time','api_extend_time'),('terminate','api_terminate'),('renew','api_renew'),('change-package','api_change_package'),('add-quota','api_add_quota'),('usage-history','api_usage_history'),('add-wallet','api_add_wallet'),('deduct-wallet','api_deduct_wallet'),('disconnect','api_disconnect')]:
        app.add_url_rule('/api/actions/'+action,ep,ns[ep],methods=['GET'] if action=='usage-history' else ['POST'])
    app.before_request(lambda:ns['guard_subscriber_permissions']() if request.path.startswith('/subscribers') else None)
    app.before_request(ns['guard_account_action_permissions'])
    spec=importlib.util.spec_from_file_location('subscriber_csrf',ROOT/'core/license_security.py')
    security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security)
    security.install_csrf(app,security.protects_admin_account_request)
    db.execute("INSERT INTO wisp_subscribers(id,username,password,full_name,package_id,service_type,status,balance,extra_quota_mb,snap_volume_quota_mb,last_renewed_at,expires_at) VALUES(1,'fixture-user','fixture-password','اختبار',1,'hotspot','active',100,5,123,'2026-10-10 12:00:00','2026-11-01 12:00:31')")
    db.execute("INSERT INTO wisp_vouchers VALUES(1,'fixture-card','كرت اختبار',100,1)")
    db.commit()
    return app,db,ns

class SubscriberSafetyTests(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns=fixture();self.client=self.app.test_client()
        self.login(['subscribers.view','subscribers.create','subscribers.edit','subscribers.renew','subscribers.delete','subscribers.disconnect','wallets.manage'])
        self.license=types.ModuleType('services.license_guard_service')
        self.license.get_active_license_status=lambda:dict(valid=True,status='active')
        self.patch=patch.dict(sys.modules,{'services.license_guard_service':self.license});self.patch.start()
    def tearDown(self):self.patch.stop();self.db.close()
    def login(self,permissions):
        with self.client.session_transaction() as s:
            s['manager']={'id':2,'role_id':2,'username':'fixture-manager'};s['permissions']=permissions;s['_license_csrf']='fixture-token'
    def post(self,path,data=None,token='fixture-token',json=None):
        return self.client.post(path,data=data,json=json,headers={'X-CSRF-Token':token} if token else {})
    def profile(self,**kw):return dict(password='new-password',full_name='اسم معدل',phone='123',mac_binding='',static_ip='',notes='',**kw)
    def new(self,**kw):return dict(username='new-fixture',password='fixture-password',package_id='1',service_type='hotspot',full_name='جديد',**kw)
    def balance(self,kind='subscriber'):return self.db.execute('SELECT balance FROM '+('wisp_subscribers' if kind=='subscriber' else 'wisp_vouchers')+' WHERE id=1').fetchone()[0]
    def test_profile_success_and_radius_same_transaction(self):
        self.ns['update_subscriber'](1,self.profile())
        self.assertEqual(self.db.execute('SELECT password FROM wisp_subscribers WHERE id=1').fetchone()[0],'new-password')
        self.assertEqual(self.db.execute("SELECT value FROM radcheck WHERE attribute='Cleartext-Password'").fetchone()[0],'new-password')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM user_audit_logs').fetchone()[0],1)
    def test_update_radius_failure_rolls_back_profile_and_radius(self):
        real=self.ns['sync_subscriber_to_radius']
        def fail(*a,**kw):real(*a,**kw);raise RuntimeError('injected')
        self.ns['sync_subscriber_to_radius']=fail
        with self.assertRaises(RuntimeError):self.ns['update_subscriber'](1,self.profile())
        self.assertEqual(self.db.execute('SELECT password FROM wisp_subscribers WHERE id=1').fetchone()[0],'fixture-password')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM radcheck').fetchone()[0],0)
    def test_create_radius_false_rolls_back_account(self):
        self.ns['sync_subscriber_to_radius']=lambda *a,**kw:False
        with self.assertRaises(ValueError):self.ns['create_subscriber'](self.new())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_subscribers').fetchone()[0],1)
    def test_create_active_and_suspended_and_unlimited(self):
        for state in ['active','suspended']:
            data=dict(self.new(),username='new-'+state,status=state)
            ident=self.ns['create_subscriber'](data)
            self.assertIsNone(self.db.execute('SELECT expires_at FROM wisp_subscribers WHERE id=?',(ident,)).fetchone()[0])
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM radcheck WHERE username=?',('new-'+state,)).fetchone()[0],1 if state=='active' else 0)
    def test_profile_cannot_change_contract_fields(self):
        for field,value in [('package_id','2'),('service_type','pppoe'),('status','suspended'),('expires_at','')]:
            with self.subTest(field=field),self.assertRaises(ValueError):self.ns['update_subscriber'](1,self.profile(**{field:value}))
        self.assertEqual(self.db.execute('SELECT password FROM wisp_subscribers WHERE id=1').fetchone()[0],'fixture-password')
    def test_legacy_unchanged_fields_preserve_contract_exactly(self):
        before=tuple(self.db.execute('SELECT package_id,status,expires_at,extra_quota_mb,snap_volume_quota_mb,last_renewed_at FROM wisp_subscribers WHERE id=1').fetchone())
        self.ns['update_subscriber'](1,self.profile(package_id='1',service_type='hotspot',status='active',expires_at='2026-11-01T12:00'))
        self.assertEqual(tuple(self.db.execute('SELECT package_id,status,expires_at,extra_quota_mb,snap_volume_quota_mb,last_renewed_at FROM wisp_subscribers WHERE id=1').fetchone()),before)
    def test_wallet_success_invoice_audit_and_contract_preserved(self):
        before=tuple(self.db.execute('SELECT package_id,status,expires_at,last_renewed_at FROM wisp_subscribers WHERE id=1').fetchone())
        self.assertTrue(self.ns['action_add_wallet_balance']('subscriber',1,'0.25')[0])
        self.assertTrue(self.ns['action_deduct_wallet_balance']('subscriber',1,'10.25')[0])
        self.assertEqual(self.balance(),90)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_invoices').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_audit_logs').fetchone()[0],2)
        self.assertEqual(tuple(self.db.execute('SELECT package_id,status,expires_at,last_renewed_at FROM wisp_subscribers WHERE id=1').fetchone()),before)
    def test_wallet_invoice_or_audit_failure_rolls_back(self):
        real=self.ns['execute_in_transaction']
        for failed_table in ['wisp_invoices','wisp_audit_logs']:
            for operation in ['action_add_wallet_balance','action_deduct_wallet_balance']:
                def fail(conn,sql,params=(),fetch=None):
                    if 'INSERT INTO '+failed_table in sql:raise RuntimeError('injected')
                    return real(conn,sql,params,fetch)
                self.ns['execute_in_transaction']=fail
                with self.assertRaises(RuntimeError):self.ns[operation]('subscriber',1,10)
                self.assertEqual(self.balance(),100)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_invoices').fetchone()[0],0)
        self.ns['execute_in_transaction']=real
    def test_wallet_rejects_invalid_and_insufficient_without_writes(self):
        for amount in ['NaN','Infinity','-1','0','0.001','100000000']:
            self.assertFalse(self.ns['action_add_wallet_balance']('subscriber',1,amount)[0])
        self.assertFalse(self.ns['action_deduct_wallet_balance']('subscriber',1,101)[0])
        self.assertEqual(self.balance(),100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_audit_logs').fetchone()[0],0)
    def test_voucher_wallet_audit_rollback_no_subscriber_invoice(self):
        self.assertTrue(self.ns['action_add_wallet_balance']('voucher',1,1)[0])
        self.assertEqual(self.balance('voucher'),101)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_invoices').fetchone()[0],0)
    def test_wallet_locked_current_balance_beats_stale_resolution(self):
        self.ns['get_target_entity']=lambda *a:({'id':1,'username':'fixture-user','balance':100},'subscriber')
        self.assertTrue(self.ns['action_deduct_wallet_balance']('subscriber',1,80)[0])
        self.assertFalse(self.ns['action_deduct_wallet_balance']('subscriber',1,80)[0])
        self.assertEqual(self.balance(),20)
    def test_post_requires_csrf_even_for_authorized_manager(self):
        for path,data in [('/subscribers/add',self.new()),('/subscribers/edit/1',self.profile()),('/api/actions/add-wallet',dict(target_type='subscriber',target_id=1,amount=1))]:
            self.assertEqual(self.post(path,data,token=None).status_code,403)
        self.assertEqual(self.balance(),100)
    def test_read_only_manager_cannot_create_edit_or_execute_actions(self):
        self.login(['subscribers.view'])
        for path,data in [('/subscribers/add',self.new()),('/subscribers/edit/1',self.profile())]:
            self.assertEqual(self.post(path,data).status_code,403)
        for action in ['delete','extend-time','terminate','renew','change-package','add-quota','add-wallet','deduct-wallet','disconnect']:
            self.assertEqual(self.post('/api/actions/'+action,dict(target_type='subscriber',target_id=1)).status_code,403)
        self.assertEqual(self.ns['action_calls'],[])
    def test_permissions_are_specific_to_operation_and_account_kind(self):
        self.login(['subscribers.renew'])
        self.assertTrue(self.post('/api/actions/renew',dict(target_type='subscriber',target_id=1)).json['success'])
        self.assertEqual(self.post('/api/actions/change-package',dict(target_type='subscriber',target_id=1)).status_code,403)
        self.assertEqual(self.post('/api/actions/renew',dict(target_type='voucher',target_id=1)).status_code,403)
        self.login(['vouchers.actions'])
        self.assertTrue(self.post('/api/actions/renew',dict(target_type='voucher',target_id=1)).json['success'])
        self.assertEqual(self.post('/api/actions/add-wallet',dict(target_type='voucher',target_id=1,amount=1)).status_code,403)
    def test_wallet_api_preserves_actor_and_success(self):
        self.assertTrue(self.post('/api/actions/add-wallet',dict(target_type='subscriber',target_id=1,amount='1.25')).json['success'])
        self.assertEqual(self.balance(),101.25)
        self.assertEqual(self.db.execute('SELECT username FROM wisp_audit_logs').fetchone()[0],'fixture-manager')
    def test_unauthenticated_and_invalid_kind_do_not_execute(self):
        with self.client.session_transaction() as s:s.pop('manager')
        self.assertEqual(self.post('/api/actions/renew',dict(target_type='subscriber',target_id=1)).status_code,401)
        self.login(['subscribers.renew'])
        self.assertEqual(self.post('/api/actions/renew',dict(target_type='invalid',target_id=1)).status_code,400)
    def test_create_form_rendering_and_token(self):
        response=self.client.get('/subscribers/create')
        self.assertEqual(response.status_code,200)
        self.assertIn('name="csrf_token"',response.get_data(as_text=True))
    def test_profile_template_has_no_contract_inputs_and_hides_readonly_controls(self):
        context=dict(subscriber=dict(id=1,username='fixture-user',password='fixture-password',full_name='اختبار',package_name='باقة',status='active',is_online=True,rate_download='4M',rate_upload='2M',service_type='hotspot'),
            sessions=[],invoices=[],audit_logs=[],analytics=dict(has_data=False),analytics_json='{"has_data":false}',usage_percent=0)
        from flask import render_template
        with self.app.test_request_context('/subscribers/1'):
            session['permissions']=['subscribers.edit','subscribers.disconnect']
            html=render_template('subscriber_details.html',**context)
            for field in ['package_id','status','expires_at','service_type']:self.assertNotIn('name="'+field+'"',html)
            self.assertIn('form="editSubForm"',html)
            session['permissions']=['subscribers.view']
            html=render_template('subscriber_details.html',**context)
            self.assertNotIn('id="editSubForm"',html);self.assertNotIn('onclick="disconnectUser(',html)

if __name__=='__main__':unittest.main()
