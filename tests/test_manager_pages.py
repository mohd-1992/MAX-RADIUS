"""Authorization, accounting and UI regressions using isolated SQLite/Flask only."""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from html.parser import HTMLParser
from manager_page_fixture import fixture, ROOT


class ManagerSafety(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns,self.stubs,self.seed,self.wallet=fixture()
        self.client=self.app.test_client()
        self.login(['managers.view','managers.manage','managers.billing','roles.manage'])

    def tearDown(self): self.db.close()

    def login(self,perms=(),ident=2,original=None):
        with self.client.session_transaction() as s:
            s.clear();s.update(admin_id=ident,admin_username='audit2',permissions=list(perms),_license_csrf='test-token')
            if original:s['original_admin_id']=original

    def post(self,path,**values):
        values.setdefault('csrf_token','test-token')
        values.setdefault('request_id','reference_test_0123456789')
        with patch.dict(sys.modules,self.stubs):return self.client.post(path,data=values)

    def balance(self,ident=2):return self.ns['query_one']('SELECT wallet_balance FROM wisp_managers WHERE id=?',(ident,))['wallet_balance']

    def count(self):return self.ns['query_one']('SELECT COUNT(*) n FROM wisp_manager_invoices')['n']

    def test_exit_does_not_promote_restricted_account(self):
        self.login()
        self.assertEqual(self.client.get('/impersonate/exit').status_code,405)
        self.assertEqual(self.post('/impersonate/exit').status_code,403)
        with self.client.session_transaction() as s:self.assertEqual(s['admin_id'],2)

    def test_impersonation_valid_original_restored(self):
        self.login(original=1)
        self.assertEqual(self.post('/impersonate/exit').status_code,302)
        with self.client.session_transaction() as s:
            self.assertEqual(s['admin_id'],1);self.assertNotIn('original_admin_id',s)
            self.assertEqual(s['admin_username'],'audit1')

    def test_impersonation_invalid_original_clears_session(self):
        for ident in (2,999):
            self.login(original=ident);self.assertEqual(self.post('/impersonate/exit').status_code,302)
            with self.client.session_transaction() as s:self.assertNotIn('admin_id',s)

    def test_disabled_original_is_not_restored(self):
        self.db.execute('UPDATE wisp_managers SET is_active=0 WHERE id=1');self.db.commit()
        self.login(original=1);self.post('/impersonate/exit')
        with self.client.session_transaction() as s:self.assertNotIn('admin_id',s)

    def test_routes_require_permissions(self):
        self.login()
        for path in ['/reseller-wallets','/api/roles/2/permissions']:
            with patch.dict(sys.modules,self.stubs):self.assertEqual(self.client.get(path).status_code,302 if path=='/reseller-wallets' else 403)
        for path,data in [('/api/reseller-wallets/deposit',dict(reseller_id=2,amount=10)),('/api/reseller-wallets/transfer',dict(from_id=2,to_id=3,amount=10))]:
            self.assertEqual(self.post(path,**data).status_code,403)
        self.assertEqual(self.balance(),100);self.assertEqual(self.count(),0)

    def test_financial_csrf_all_entrypoints(self):
        for path in ['/managers/2/deposit','/roles/add','/managers/2/impersonate','/impersonate/exit','/api/reseller-wallets/deposit','/api/reseller-wallets/transfer']:
            self.assertEqual(self.client.post(path,data={'amount':10}).status_code,403)
        self.assertEqual(self.balance(),100)

    def test_anonymous_denied(self):
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.post('/api/reseller-wallets/deposit',data={'amount':10}).status_code,401)

    def test_deposit_reference_replay_applied_once(self):
        for _ in range(2):self.post('/managers/2/deposit',amount=10)
        self.assertEqual(self.balance(),110);self.assertEqual(self.count(),1)

    def test_cross_entrypoint_replay_applied_once(self):
        self.post('/managers/2/deposit',amount=10)
        self.assertTrue(self.post('/api/reseller-wallets/deposit',reseller_id=2,amount=10).json['success'])
        self.assertEqual(self.balance(),110);self.assertEqual(self.count(),1)

    def test_changed_replay_rejected(self):
        self.post('/managers/2/deposit',amount=10)
        r=self.post('/api/reseller-wallets/deposit',reseller_id=2,amount=20)
        self.assertEqual(r.status_code,400);self.assertEqual(self.balance(),110)

    def test_missing_reference_rejected(self):
        self.client.post('/managers/2/deposit',data={'amount':10,'csrf_token':'test-token'})
        self.assertEqual(self.balance(),100)

    def test_invalid_amounts_rejected_without_writes(self):
        for amount in ['-1','0','NaN','Infinity','abc','0.001','10000000000']:
            with self.subTest(amount=amount):
                self.assertEqual(self.post('/api/reseller-wallets/deposit',reseller_id=2,amount=amount).status_code,400)
        self.assertEqual(self.balance(),100);self.assertEqual(self.count(),0)

    def test_self_transfer_rejected(self):
        self.assertEqual(self.post('/api/reseller-wallets/transfer',from_id=2,to_id=2,amount=20).status_code,400)
        self.assertEqual(self.balance(),100);self.assertEqual(self.count(),0)

    def test_transfer_conserves_money_and_replay(self):
        for _ in range(2):self.assertTrue(self.post('/api/reseller-wallets/transfer',from_id=2,to_id=3,amount=20).json['success'])
        self.assertEqual((self.balance(2),self.balance(3)),(80,120));self.assertEqual(self.count(),2)
        self.assertEqual(self.ns['query_one']('SELECT SUM(wallet_balance) n FROM wisp_managers')['n'],300)

    def test_transfer_missing_receiver_insufficient_funds(self):
        for target,amount in [(999,10),(3,101)]:
            self.assertEqual(self.post('/api/reseller-wallets/transfer',from_id=2,to_id=target,amount=amount).status_code,400)
        self.assertEqual(self.balance(),100);self.assertEqual(self.count(),0)

    def test_disabled_deleted_accounts_blocked(self):
        for column in ['is_active','is_deleted']:
            self.db.execute('UPDATE wisp_managers SET is_active=1,is_deleted=0')
            self.db.execute('UPDATE wisp_managers SET '+column+'=? WHERE id=3',(0 if column=='is_active' else 1,));self.db.commit()
            self.assertEqual(self.post('/api/reseller-wallets/deposit',reseller_id=3,amount=10).status_code,400)
            self.assertEqual(self.post('/api/reseller-wallets/transfer',from_id=2,to_id=3,amount=10).status_code,400)
        self.assertEqual(self.count(),0)

    def test_wallet_and_manager_finance_use_same_balance(self):
        self.post('/api/reseller-wallets/deposit',reseller_id=2,amount=25)
        data=self.wallet['get_wallets_overview']()
        self.assertEqual(next(w['balance'] for w in data['wallets'] if w['reseller_id']==2),125)
        self.assertEqual(self.ns['query_one']('SELECT balance FROM wisp_reseller_wallets WHERE reseller_id=2')['balance'],100)
        self.assertEqual(self.ns['query_one']('SELECT balance FROM wisp_resellers WHERE id=2')['balance'],500)

    def test_credit_ceiling_and_zero_unlimited(self):
        self.ns['deposit_manager_wallet'](2,50,'credit')
        with self.assertRaises(ValueError):self.ns['deposit_manager_wallet'](2,0.01,'credit')
        self.ns['settle_manager_debt'](2,20)
        self.ns['deposit_manager_wallet'](2,20,'credit')
        self.db.execute('UPDATE wisp_managers SET credit_limit=0 WHERE id=3');self.db.commit()
        self.ns['deposit_manager_wallet'](3,1000,'credit')
        self.assertEqual(self.balance(3),1100)

    def test_debt_current_independent_of_filter(self):
        self.ns['deposit_manager_wallet'](2,50,'credit');self.ns['settle_manager_debt'](2,20)
        _,invoices,stats=self.ns['get_manager_statement'](2,transaction_type='debt_payment')
        self.assertEqual(len(invoices),1);self.assertEqual(stats['net_outstanding_debt'],30)

    def test_debt_repayment_reference_not_duplicated(self):
        self.ns['deposit_manager_wallet'](2,50,'credit')
        for _ in range(2):self.post('/managers/2/settle-debt',amount=20)
        self.assertEqual(self.count(),2);self.assertEqual(self.ns['get_manager_by_id'](2)['net_outstanding_debt'],30)

    def test_overpayment_and_overdraft_rejected(self):
        self.ns['deposit_manager_wallet'](2,30,'credit')
        with self.assertRaises(ValueError):self.ns['settle_manager_debt'](2,31)
        with self.assertRaises(ValueError):self.ns['deduct_manager_wallet'](2,131)
        self.assertEqual(self.balance(),130)

    def test_manager_validation_rejects_unknown_role_package_and_nonfinite(self):
        for data in [dict(role_id=999),dict(allowed_packages=[999]),dict(credit_limit='NaN'),dict(commission_percent=101),dict(commission_percent=-1),dict(full_name='')]:
            values=dict(full_name='Fixture',role_id=2,allowed_packages=[1]);values.update(data)
            with self.subTest(data=data),self.assertRaises(ValueError):self.ns['update_manager'](2,values)
        self.assertEqual(self.ns['get_manager_by_id'](2)['full_name'],'Audit 2')

    def test_disabled_and_deleted_wallet_counts(self):
        self.db.execute('UPDATE wisp_managers SET is_active=0 WHERE id=2');self.db.commit()
        data=self.wallet['get_wallets_overview']()
        self.assertEqual(data['active_count'],2)
        self.assertEqual(next(w['wallet_status'] for w in data['wallets'] if w['reseller_id']==2),'frozen')

    def test_update_manager_rolls_back_legacy_write_failure(self):
        original=self.ns['_manager_tx']
        def fail(conn,sql,args=(),fetch=None):
            if 'UPDATE wisp_admins' in sql:raise RuntimeError('isolated failure')
            return original(conn,sql,args,fetch)
        with patch.dict(self.ns,{'_manager_tx':fail}),self.assertRaises(RuntimeError):self.ns['update_manager'](2,dict(full_name='Changed',role_id=2,allowed_packages=[1]))
        self.assertEqual(self.ns['get_manager_by_id'](2)['full_name'],'Audit 2')

    def test_ordinary_deposit_and_deduction_void(self):
        _,ref=self.ns['deposit_manager_wallet'](2,10)
        inv=self.ns['query_one']('SELECT id FROM wisp_manager_invoices WHERE invoice_number=?',(ref,))
        self.ns['void_manager_invoice'](inv['id'],'Fixture');self.assertEqual(self.balance(),100)
        _,ref=self.ns['deduct_manager_wallet'](2,20)
        inv=self.ns['query_one']('SELECT id FROM wisp_manager_invoices WHERE invoice_number=?',(ref,))
        self.ns['void_manager_invoice'](inv['id'],'Fixture');self.assertEqual(self.balance(),100)
        with self.assertRaises(ValueError):self.ns['void_manager_invoice'](inv['id'],'Fixture again')

    def test_partly_repaid_credit_void_rejected(self):
        _,ref=self.ns['deposit_manager_wallet'](2,50,'credit');self.ns['settle_manager_debt'](2,10)
        inv=self.ns['query_one']('SELECT id FROM wisp_manager_invoices WHERE invoice_number=?',(ref,))
        with self.assertRaises(ValueError):self.ns['void_manager_invoice'](inv['id'],'Fixture')
        self.assertEqual(self.ns['get_manager_by_id'](2)['net_outstanding_debt'],40)

    def test_deleted_account_cannot_be_reactivated(self):
        self.db.execute('UPDATE wisp_managers SET is_deleted=1,is_active=0 WHERE id=2');self.db.commit()
        with self.assertRaises(ValueError):self.ns['toggle_manager_status'](2)
        self.assertEqual(self.ns['get_manager_by_id'](2)['is_active'],0)

    def test_javascript_role_load_failure_blocks_edit_and_quick_deposit_collects_value(self):
        # Execute actual template JS in Node with UI/network stubs, no real POST.
        html=self.client.get('/roles').data.decode()
        js=html.split('let roleLoadVersion')[1].split('</script>')[0]
        code='''let roleLoadVersion'''+js+'''
        let visible=false,alerted=false;
        const els={};global.document={getElementById:id=>els[id]||(els[id]={value:'',classList:{add:()=>{visible=false},remove:()=>{visible=true}}}),querySelectorAll:()=>[]};
        global.fetch=()=>Promise.reject(new Error('isolated'));
        global.Swal={fire:()=>{alerted=true}};
        openEditRoleModal(2,"O'Name",'',0,'restricted2');
        setImmediate(()=>{if(visible||!alerted||els.role_code_input.value!=='restricted2'||!els.role_code_input.disabled)process.exit(1);});
        '''
        r=subprocess.run([shutil.which('node'),'-e',code],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        with patch.dict(sys.modules,self.stubs):html=self.client.get('/reseller-wallets').data.decode()
        js=html[html.index('    function quickDeposit'):].split('</script>')[0]
        code='''let collected; global.managerRequestId=()=> 'fixture-reference';
        global.Swal={showValidationMessage:()=>{},fire:opts=>{collected=opts.preConfirm('12');return {then:()=>{}}}};
        '''+js+'''quickDeposit(2,"O'Name");if(collected!=='12')process.exit(1);'''
        r=subprocess.run([shutil.which('node'),'-e',code],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_role_invalid_ids_preserve_prior_permissions(self):
        self.db.execute('INSERT INTO wisp_role_permissions VALUES(2,1)');self.db.commit()
        for values in [['bad'],[999]]:
            with self.assertRaises(ValueError):self.ns['update_role'](2,dict(name='New name'),values)
        self.assertEqual(self.ns['get_role_permission_ids'](2),[1])
        self.assertEqual(self.ns['get_role_by_id'](2)['name'],'Role 2')

    def test_role_create_update_and_immutable_code(self):
        role=self.ns['create_role'](dict(name='New',code='new_role'),[1,2])
        self.ns['update_role'](role,dict(name='Updated',code='new_role'),[3])
        self.assertEqual(self.ns['get_role_permission_ids'](role),[3])
        with self.assertRaises(ValueError):self.ns['update_role'](role,dict(name='Invalid',code='changed'),[1])

    def test_role_transaction_rollback_on_insert_failure(self):
        self.db.execute('INSERT INTO wisp_role_permissions VALUES(2,1)');self.db.commit()
        original=self.ns['_manager_tx']
        def fail(conn,sql,args=(),fetch=None):
            if 'INSERT INTO wisp_role_permissions' in sql:raise RuntimeError('isolated failure')
            return original(conn,sql,args,fetch)
        with patch.dict(self.ns,{'_manager_tx':fail}),self.assertRaises(RuntimeError):self.ns['update_role'](2,dict(name='New'),[2])
        self.assertEqual(self.ns['get_role_permission_ids'](2),[1]);self.assertEqual(self.ns['get_role_by_id'](2)['name'],'Role 2')

    def manager_data(self):return dict(username='new-fixture',password='fixture-password',full_name='Fixture',role_id=2,wallet_balance=20,allowed_packages=[1])

    def test_manager_creation_atomic_success(self):
        self.stubs['services.license_guard_service'].check_manager_quota=lambda _: (True,'',0,0)
        with patch.dict(sys.modules,self.stubs):ident=self.ns['create_manager'](self.manager_data())
        self.assertEqual(self.balance(ident),20);self.assertEqual(self.count(),1)
        self.assertIsNotNone(self.ns['query_one']('SELECT id FROM wisp_admins WHERE username=?',('new-fixture',)))

    def test_manager_creation_rollback_on_legacy_failure(self):
        self.stubs['services.license_guard_service'].check_manager_quota=lambda _: (True,'',0,0)
        original=self.ns['_manager_tx']
        def fail(conn,sql,args=(),fetch=None):
            if 'INSERT INTO wisp_admins' in sql:raise RuntimeError('isolated failure')
            return original(conn,sql,args,fetch)
        with patch.dict(sys.modules,self.stubs),patch.dict(self.ns,{'_manager_tx':fail}),self.assertRaises(RuntimeError):self.ns['create_manager'](self.manager_data())
        self.assertIsNone(self.ns['query_one']('SELECT id FROM wisp_managers WHERE username=?',('new-fixture',)))
        self.assertEqual(self.count(),0)

    def test_transfer_rollback_on_second_invoice_failure(self):
        original=self.ns['_manager_tx'];calls=[]
        def fail(conn,sql,args=(),fetch=None):
            if 'INSERT INTO wisp_manager_invoices' in sql:
                calls.append(1)
                if len(calls)==2:raise RuntimeError('isolated failure')
            return original(conn,sql,args,fetch)
        with patch.dict(self.ns,{'_manager_tx':fail}),self.assertRaises(RuntimeError):self.ns['transfer_manager_wallet'](2,3,20)
        self.assertEqual((self.balance(2),self.balance(3)),(100,100));self.assertEqual(self.count(),0)

    def test_card_package_restriction_and_insufficient_balance(self):
        self.db.execute('UPDATE wisp_packages SET cost=10 WHERE id=1');self.db.commit()
        for settings in [dict(allowed_packages='[999]'),dict(wallet_balance=0),dict(is_active=0),dict(is_deleted=1)]:
            self.db.execute('UPDATE wisp_managers SET allowed_packages="[1]",wallet_balance=100,is_active=1,is_deleted=0 WHERE id=2')
            for key,val in settings.items():self.db.execute('UPDATE wisp_managers SET '+key+'=? WHERE id=2',(val,))
            self.db.commit()
            count=self.ns['query_one']('SELECT COUNT(*) n FROM wisp_voucher_batches')['n']
            with patch.dict(sys.modules,self.stubs),self.assertRaises(ValueError):self.ns['generate_voucher_batch']('Isolated',1,2,reseller_id=2)
            self.assertEqual(self.ns['query_one']('SELECT COUNT(*) n FROM wisp_voucher_batches')['n'],count)

    def test_valid_card_purchase_and_void_rejection(self):
        self.db.execute('UPDATE wisp_packages SET cost=10 WHERE id=1');self.db.commit()
        with patch.dict(sys.modules,self.stubs):self.ns['generate_voucher_batch']('Isolated',1,2,reseller_id=2)
        self.assertEqual(self.balance(),80)
        inv=self.ns['query_one']('SELECT id FROM wisp_manager_invoices')['id']
        with self.assertRaises(ValueError):self.ns['void_manager_invoice'](inv,'test')
        self.assertEqual(self.balance(),80)

    def test_transfer_single_leg_void_rejected(self):
        self.ns['transfer_manager_wallet'](2,3,20)
        inv=self.ns['query_one']('SELECT id FROM wisp_manager_invoices')['id']
        with self.assertRaises(ValueError):self.ns['void_manager_invoice'](inv,'test')
        self.assertEqual((self.balance(2),self.balance(3)),(80,120))

    def test_pagination_keeps_older_records_accessible(self):
        for i in range(105):self.ns['deposit_manager_wallet'](2,1,request_id=f'pagination-reference-{i:08}')
        self.assertEqual(len(self.ns['get_all_manager_invoices'](limit=100)),100)
        self.assertEqual(len(self.ns['get_all_manager_invoices'](limit=100,offset=100)),5)
        self.assertEqual(self.client.get('/managers/invoices?page=2').status_code,200)

    def test_all_pages_render_and_no_hash_exposure(self):
        for path in ['/managers','/roles','/managers/invoices','/managers/2/statement','/reseller-wallets']:
            with patch.dict(sys.modules,self.stubs):r=self.client.get(path)
            self.assertEqual(r.status_code,200,path);self.assertNotIn(b'AUDIT-HASH',r.data,path)

    def test_ui_hides_disallowed_write_buttons(self):
        self.login(['managers.view'])
        for path in ['/managers','/managers/2/statement','/reseller-wallets']:
            with patch.dict(sys.modules,self.stubs):r=self.client.get(path)
            html=r.data.decode()
            self.assertNotIn('onclick="openDepositModal(',html)
            self.assertNotIn('onclick="openEditManagerModal(',html)
            self.assertNotIn('onclick="openTransferModal(',html)

    def test_safe_name_scripts_and_actual_javascript(self):
        class Tags(HTMLParser):
            def __init__(self):super().__init__();self.handlers=[];self.script=False;self.inline=[]
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if a.get('onclick'):self.handlers.append(a['onclick'])
                if tag=='script':self.script=True
            def handle_endtag(self,tag):
                if tag=='script':self.script=False
            def handle_data(self,s):
                if self.script:self.inline.append(s)
        node=shutil.which('node')
        self.assertIsNotNone(node,'Node is required for actual JavaScript checks')
        self.db.execute('UPDATE wisp_managers SET full_name=? WHERE id=2',("O'Name <fixture>",));self.db.commit()
        for path in ['/managers','/roles','/reseller-wallets','/managers/invoices','/managers/2/statement']:
            with patch.dict(sys.modules,self.stubs):r=self.client.get(path)
            tags=Tags();tags.feed(r.data.decode())
            with tempfile.TemporaryDirectory() as tmp:
                file=Path(tmp)/'check.js'
                file.write_text('\n'.join(tags.inline)+'\n'+ '\n'.join('new Function('+json.dumps(h)+');' for h in tags.handlers),encoding='utf8')
                result=subprocess.run([node,'--check',str(file)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
            # Execute the actual safe-name handler without a browser or network.
            handler=next((h for h in tags.handlers if h.startswith('openDepositModal(2,')),None)
            if handler:
                runner='let captured; function openDepositModal(id,name){captured=name;} '+handler+'; if(captured!=='+json.dumps("O'Name <fixture>")+')process.exit(1);'
                result=subprocess.run([node,'-e',runner],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
