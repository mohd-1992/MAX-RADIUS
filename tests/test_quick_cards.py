import ast,datetime,json,re,secrets,string,types,unittest
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from flask import g,has_request_context,request,session
from voucher_page_fixture import voucher_fixture,ROOT
from database.quick_card_schema import install_quick_card_schema

class QuickCardsTests(unittest.TestCase):
 def setUp(self):
  self.app,self.db,self.ns,self.stubs,self.seed=voucher_fixture()
  install_quick_card_schema(self.db,mysql=False);install_quick_card_schema(self.db,mysql=False)
  self.seed('wisp_managers',id=2,username='fixture-owner',role_id=2,wallet_balance=100,is_active=1,is_deleted=0,allowed_packages='[1]')
  self.seed('wisp_managers',id=3,username='fixture-other',role_id=2,wallet_balance=100,is_active=1,is_deleted=0,allowed_packages='[1]')
  self.db.execute("UPDATE wisp_managers SET role_id=1 WHERE id=1")
  self.db.execute("UPDATE wisp_packages SET cost=8,price=10,is_active=1,service_type='hotspot' WHERE id=1");self.db.commit()
  rawone=self.ns['query_one'];rawall=self.ns['query_all']
  own=dict(json=json,re=re,g=g,has_request_context=has_request_context,request=request,
   raw_query_one=rawone,raw_query_all=rawall,get_current_manager=lambda:rawone('SELECT * FROM wisp_managers WHERE id=?',(session.get('actor',2),)))
  self.load('core/ownership.py',own)
  self.own=own
  self.quick=dict(json=json,re=re,Decimal=Decimal,db_session=self.ns['db_session'],is_mysql_conn=lambda c:False,
   raw_one=rawone,owner_id=own['owner_id'],generation_owner=own['generation_owner'],scoped_query_all=own['scoped_query_all'],scoped_query_one=own['scoped_query_one'],get_db_storage_now=self.ns['get_db_storage_now'])
  self.load('services/voucher_service.py',self.ns,['_VoucherWriter','generate_voucher_batch','generate_random_code'])
  self.quick.update(_VoucherWriter=self.ns['_VoucherWriter'],generate_voucher_batch=self.ns['generate_voucher_batch'])
  self.load('services/quick_card_service.py',self.quick)
  self.ns.update(is_mysql_conn=lambda c:False)
  mod=types.ModuleType('services.quick_card_service');mod.get_issue_policy=self.quick['get_issue_policy']
  ownmod=types.ModuleType('core.ownership');ownmod.__dict__.update(own)
  self.stubs.update({'services.quick_card_service':mod,'core.ownership':ownmod})
  self.modules=patch.dict('sys.modules',self.stubs);self.modules.start()
 def tearDown(self):
  if hasattr(self,'http_modules'):self.http_modules.stop()
  self.modules.stop();self.db.close()
 def load(self,path,ns,names=None):
  tree=ast.parse((ROOT/path).read_text(encoding='utf-8-sig'))
  nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef,ast.Assign)) and (names is None or getattr(n,'name',None) in names)]
  exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),ns)
 def issue(self,count=1,key='fixture_request_0001',actor=2,owner=None,package=1):
  with self.app.test_request_context('/vouchers/quick',method='POST'):
   session['actor']=actor
   manager=self.own['ownership_manager']()
   return self.quick['issue_quick_cards'](manager,package,count,key,owner)
 def read(self,sql,p=()):return self.ns['query_one'](sql,p)
 def policy(self,**values):
  data=dict(self.quick['DEFAULT_POLICY'],**values)
  with self.app.test_request_context('/vouchers/reseller-issue-settings'):
   session['actor']=1;return self.quick['save_issue_policy'](data)
 def test_quick_automatic_name_uses_owner_and_real_batch_id(self):
  result=self.issue(2)
  batch=self.read('SELECT name FROM wisp_voucher_batches WHERE id=?',(result['batch_id'],))
  pkg=self.read('SELECT name FROM wisp_packages WHERE id=1')['name']
  owner=self.read('SELECT username FROM wisp_managers WHERE id=2')['username']
  self.assertEqual(batch['name'],f"{pkg} — 2 كرت — {owner} — #{result['batch_id']}")
 def test_regular_automatic_name_without_owner(self):
  with self.app.test_request_context('/vouchers'):
   session['actor']=1
   ident,_=self.ns['generate_voucher_batch']('',1,1,reseller_id=None)
  batch=self.read('SELECT name FROM wisp_voucher_batches WHERE id=?',(ident,))
  pkg=self.read('SELECT name FROM wisp_packages WHERE id=1')['name']
  self.assertEqual(batch['name'],f"{pkg} — 1 كرت — #{ident}")
 def test_custom_name_and_existing_batch_remain_unchanged(self):
  with self.app.test_request_context('/vouchers'):
   session['actor']=1
   ident,_=self.ns['generate_voucher_batch']('اسم مخصص',1,1,reseller_id=2)
  self.issue(1,key='fixture_another_name_01234')
  self.assertEqual(self.read('SELECT name FROM wisp_voucher_batches WHERE id=?',(ident,))['name'],'اسم مخصص')

 def test_single_and_two_cards_charge_and_snapshot(self):
  for count in [1,2]:
   result=self.issue(count,key='fixture_request_'+str(count).zfill(5))
   self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_vouchers WHERE batch_id=?',(result['batch_id'],))['n'],count)
   self.assertEqual(float(result['amount']),count*8)
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']),76)
 def test_repeat_request_same_receipt_no_double_charge(self):
  first=self.issue(2);second=self.issue(2)
  self.assertEqual(first['id'],second['id']);self.assertTrue(second['repeated'])
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']),84)
  self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_manager_invoices')['n'],1)
 def test_reused_reference_different_payload_rejected(self):
  self.issue()
  with self.assertRaises(ValueError):self.issue(2)
 def test_zero_wallet_rolls_back_everything(self):
  self.db.execute('UPDATE wisp_managers SET wallet_balance=0 WHERE id=2');self.db.commit()
  before=self.read('SELECT COUNT(*) n FROM wisp_vouchers')['n']
  with self.assertRaises(ValueError):self.issue()
  self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_vouchers')['n'],before)
  self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_quick_card_issues')['n'],0)
 def test_receipt_failure_rolls_back_batch_wallet_radius_invoice(self):
  self.db.execute("CREATE TRIGGER receipt_failure BEFORE INSERT ON wisp_quick_card_issues BEGIN SELECT RAISE(ABORT,'fixture'); END;")
  before=self.read('SELECT COUNT(*) n FROM radcheck')['n']
  with self.assertRaises(Exception):self.issue()
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']),100)
  self.assertEqual(self.read('SELECT COUNT(*) n FROM radcheck')['n'],before)
  self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_manager_invoices')['n'],0)
 def test_forged_owner_forced_and_forbidden_package_rejected(self):
  result=self.issue(owner=3);self.assertEqual(result['reseller_id'],2)
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=3')['wallet_balance']),100)
  with self.assertRaises(PermissionError):self.issue(package=999,key='fixture_request_9999')
 def test_global_policy_three_formats_numbers_and_mixed(self):
  for n,fmt in enumerate(['pin_only','same_user_pass','user_pass']):
   for char in ['numbers','alphanumeric']:
    prefix='25' if char=='numbers' else 'AB'
    self.policy(prefix=prefix,char_type=char,total_length=10,format_type=fmt,password_length=6)
    result=self.issue(key='fixture_'+fmt+'_'+char)
    c=self.read('SELECT username,password FROM wisp_vouchers WHERE batch_id=?',(result['batch_id'],))
    self.assertTrue(c['username'].startswith(prefix));self.assertEqual(len(c['username']),10)
    if fmt!='user_pass':self.assertEqual(c['username'],c['password'])
    else:self.assertEqual(len(c['password']),6)
 def test_policy_cannot_be_saved_by_reseller(self):
  with self.app.test_request_context('/vouchers/reseller-issue-settings'):
   session['actor']=2
   with self.assertRaises(PermissionError):self.quick['save_issue_policy']({})
 def test_invalid_policy_and_bad_count_and_key(self):
  for data in [dict(prefix='ABC',char_type='numbers'),dict(total_length=5,prefix='123'),dict(total_length='NaN'),dict(format_type='evil'),dict(prefix='<script>')]:
   with self.assertRaises(ValueError):self.quick['validate_issue_policy'](data)
  for count in [0,-1,51,'1.5',True]:
   with self.assertRaises(ValueError):self.issue(count,key='fixture_invalid_001')
  with self.assertRaises(ValueError):self.issue(key='bad')
 def test_inactive_package_rejected(self):
  self.db.execute('UPDATE wisp_packages SET is_active=0 WHERE id=1');self.db.commit()
  with self.assertRaises(ValueError):self.issue()
 def test_history_search_owner_scope_and_reprint_no_charge(self):
  mine=self.issue();other=self.issue(actor=3)
  with self.app.test_request_context('/vouchers/quick/history'):
   session['actor']=2
   rows,total=self.quick['quick_issue_history']();self.assertEqual(total,1);self.assertEqual(rows[0]['id'],mine['id'])
   self.assertIsNone(self.quick['get_quick_issue'](other['id']))
   with self.assertRaises(PermissionError):self.quick['record_quick_print'](other['id'])
   for n in range(2):self.quick['record_quick_print'](mine['id'])
   self.assertEqual(self.quick['get_quick_issue'](mine['id'])['print_requests'],2)
   name=self.read('SELECT username FROM wisp_vouchers WHERE batch_id=?',(mine['batch_id'],))['username']
   self.assertEqual(self.quick['quick_issue_history'](name)[1],1)
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']),92)
 def test_admin_scope_all_and_without_owner_no_wallet_charge(self):
  self.issue();self.issue(actor=3);admin=self.issue(actor=1)
  self.assertEqual(float(admin['amount']),0)
  with self.app.test_request_context('/vouchers/quick/history'):
   session['actor']=1;self.assertEqual(self.quick['quick_issue_history']()[1],3)
 def test_standard_batch_reseller_policy_forced_admin_unchanged(self):
  self.policy(prefix='AB',char_type='alphanumeric',total_length=10,format_type='same_user_pass')
  with self.app.test_request_context('/vouchers/generate'):
   session['actor']=2
   bid,_=self.ns['generate_voucher_batch']('fixture',1,1,prefix='evil',code_length=7,same_user_pass=False,pin_only=False)
  c=self.read('SELECT username,password FROM wisp_vouchers WHERE batch_id=?',(bid,));self.assertTrue(c['username'].startswith('AB'));self.assertEqual(len(c['username']),10);self.assertEqual(c['username'],c['password'])
  with self.app.test_request_context('/vouchers/generate'):
   session['actor']=1
   bid,_=self.ns['generate_voucher_batch']('fixture',1,1,prefix='ADMIN',code_length=7,pin_only=False)
  c=self.read('SELECT username FROM wisp_vouchers WHERE batch_id=?',(bid,));self.assertTrue(c['username'].startswith('ADMIN'));self.assertEqual(len(c['username']),12)

 def test_receipt_survives_card_deletion_for_history(self):
  result=self.issue();self.db.execute('DELETE FROM wisp_vouchers WHERE batch_id=?',(result['batch_id'],));self.db.execute('DELETE FROM wisp_voucher_batches WHERE id=?',(result['batch_id'],));self.db.commit()
  with self.app.test_request_context('/vouchers/quick/history'):
   session['actor']=2;rows,total=self.quick['quick_issue_history']();self.assertEqual(total,1);self.assertEqual(rows[0]['available_cards'],0)
 def test_committed_request_replay_after_package_deactivated(self):
  first=self.issue();self.db.execute('UPDATE wisp_packages SET is_active=0 WHERE id=1');self.db.commit()
  second=self.issue();self.assertEqual(first['id'],second['id']);self.assertTrue(second['repeated'])
 def test_exhausted_code_space_rollback_does_not_charge(self):
  self.ns['generate_random_code']=lambda length,char_type:'12345678'
  self.issue();before=float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance'])
  with self.assertRaises(ValueError):self.issue(key='another_collision_request01')
  self.assertEqual(float(self.read('SELECT wallet_balance FROM wisp_managers WHERE id=2')['wallet_balance']),before)

 def setup_http(self):
  from flask import jsonify,redirect,url_for,flash,render_template,abort,Blueprint
  from core.permission_catalog import required_detail,ACTION_ENDPOINTS,permission_rows
  grants={'vouchers.generate','vouchers.view','vouchers.print'}|{r['code'] for r in permission_rows() if r['code'].startswith('detail.vouchers.quick')}
  self.grants=grants
  ns=dict(self.own,request=request,jsonify=jsonify,redirect=redirect,url_for=url_for,render_template=render_template,required_detail=required_detail,ACTION_ENDPOINTS=ACTION_ENDPOINTS,
   has_permission=lambda code,manager=None: (manager or self.own['ownership_manager']()).get('role_id')==1 or code in grants)
  exec((ROOT/'web/endpoint_permissions.py').read_text(encoding='utf-8-sig'),ns)
  endpoint_mod=types.ModuleType('web.endpoint_permissions');endpoint_mod.ENDPOINT_PERMISSIONS=ns['ENDPOINT_PERMISSIONS']
  self.http_modules=patch.dict('sys.modules',{'web.endpoint_permissions':endpoint_mod});self.http_modules.start()
  self.load('web/access_policy.py',ns)
  self.app.before_request_funcs[None]=[]
  self.app.context_processor(lambda:dict(is_reseller_scope=lambda:self.own['owner_id']() is not None,can_access_endpoint=ns['can_access_endpoint']))
  ns['install_visibility_policy'](self.app)
  security=dict(secrets=secrets,re=re,__import__=__import__)
  import hmac
  security['hmac']=hmac
  self.load('core/license_security.py',security,['install_csrf','protects_admin_account_request'])
  security['install_csrf'](self.app,security['protects_admin_account_request'])
  route=dict(self.quick,Blueprint=Blueprint,__name__='fixture.quick',logging=__import__('logging'),request=request,jsonify=jsonify,redirect=redirect,url_for=url_for,flash=flash,render_template=render_template,abort=abort,secrets=secrets,
   logger=__import__('logging').getLogger('isolated-quick'),get_current_manager=self.own['ownership_manager'],require_owned=self.own['require_owned'],scoped_query_all=self.own['scoped_query_all'],get_vouchers=self.ns['get_vouchers'],get_all_card_designs=lambda:[])
  self.load('web/routes/quick_cards.py',route)
  for name,path,methods in [('quick_cards_issue','/vouchers/quick/issue',['POST']),('quick_cards_details','/vouchers/quick/<int:issue_id>',['GET']),('quick_cards_print','/vouchers/quick/<int:issue_id>/print',['POST']),('reseller_issue_settings','/vouchers/reseller-issue-settings',['GET','POST'])]:
   self.app.add_url_rule(path,name,route[name],methods=methods)
  self.client=self.app.test_client()
  with self.client.session_transaction() as sess:sess['actor']=2;sess['_license_csrf']='fixture-token'
 def test_http_csrf_and_permission_enforcement_before_issuance(self):
  self.setup_http();data=dict(package_id=1,count=1,request_key='http_fixture_request01')
  self.assertEqual(self.client.post('/vouchers/quick/issue',data=data).status_code,403)
  data['csrf_token']='fixture-token';self.grants.remove('detail.vouchers.quick_issue')
  self.assertEqual(self.client.post('/vouchers/quick/issue',data=data).status_code,403)
  self.assertEqual(self.read('SELECT COUNT(*) n FROM wisp_quick_card_issues')['n'],0)
  self.grants.add('detail.vouchers.quick_issue')
  first=self.client.post('/vouchers/quick/issue',data=data);self.assertEqual(first.status_code,200)
  second=self.client.post('/vouchers/quick/issue',data=data);self.assertTrue(second.json['repeated']);self.assertEqual(first.json['issue_id'],second.json['issue_id'])
 def test_http_foreign_receipt_and_global_settings_denied(self):
  other=self.issue(actor=3);self.setup_http()
  self.assertEqual(self.client.get('/vouchers/quick/'+str(other['id'])).status_code,404)
  self.assertEqual(self.client.post('/vouchers/quick/'+str(other['id'])+'/print',data={'csrf_token':'fixture-token'}).status_code,404)
  self.assertEqual(self.client.get('/vouchers/reseller-issue-settings').status_code,403)
 def test_http_no_raw_error_on_bad_input(self):
  self.setup_http()
  response=self.client.post('/vouchers/quick/issue',data={'package_id':'x','count':1,'request_key':'invalid','csrf_token':'fixture-token'})
  self.assertIn(response.status_code,(400,403));self.assertNotIn('Traceback',response.get_data(as_text=True))

if __name__=='__main__':unittest.main()
