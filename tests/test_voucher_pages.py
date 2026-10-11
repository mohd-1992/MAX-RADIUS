"""Card administration regressions on disposable SQLite, without app workers or NAS."""
import ast
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from flask import session
from voucher_page_fixture import voucher_fixture,ROOT
from html.parser import HTMLParser

class Elements(HTMLParser):
    def __init__(self,html):super().__init__();self.items=[];self.feed(html)
    def handle_starttag(self,tag,attrs):self.items.append((tag,dict(attrs)))

class VoucherPageTests(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns,self.stubs,self.seed=voucher_fixture()
        self.modules=patch.dict('sys.modules',self.stubs);self.modules.start()
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:
            s['manager']={'id':2,'role_id':2};s['permissions']=['vouchers.view','vouchers.generate','vouchers.actions','vouchers.print','vouchers.delete'];s['_license_csrf']='fixture-token'
    def tearDown(self):self.modules.stop();self.db.close()
    def row(self,sql,args=()):
        row=self.db.execute(sql,args).fetchone();return dict(row) if row else None
    def post(self,path,**kwargs):return self.client.post(path,headers={'X-CSRF-Token':'fixture-token'},**kwargs)
    def context(self,path,function,*args):
        render=self.ns['render_template'];self.ns['render_template']=lambda template,**context:context
        try:
            with self.app.test_request_context(path):return self.ns[function](*args)
        finally:self.ns['render_template']=render
    def test_card_write_requires_csrf(self):
        r=self.client.post('/vouchers/update-card',data={'card_id':2,'status':'suspended'})
        self.assertEqual(r.status_code,403);self.assertEqual(self.row('SELECT status FROM wisp_vouchers WHERE id=2')['status'],'active')
    def test_card_and_design_permissions(self):
        with self.client.session_transaction() as s:s['permissions']=['vouchers.view']
        for path,payload in [('/vouchers/update-card',{'card_id':2}),('/api/vouchers/designs/save',{'name':'fixture'})]:
            self.assertEqual(self.post(path,json=payload).status_code,403)
    def test_export_and_delete_permissions_differ(self):
        with self.client.session_transaction() as s:s['permissions']=['vouchers.actions']
        for action in ['export','delete']:
            self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':action,'batch_ids':[1]}).status_code,403)
    def test_generation_invoice_failure_rolls_back_wallet_batch(self):
        self.db.execute('UPDATE wisp_packages SET cost=8 WHERE id=1');self.db.commit()
        cls=self.ns['_VoucherWriter'];write=cls.write
        def fail(tx,statement,params=()):
            if 'INSERT INTO wisp_manager_invoices' in statement:raise RuntimeError('fixture invoice failure')
            return write(tx,statement,params)
        with patch.object(cls,'write',fail),self.assertRaises(RuntimeError):self.ns['generate_voucher_batch']('fixture',1,2,reseller_id=1)
        self.assertEqual(self.row('SELECT wallet_balance FROM wisp_managers WHERE id=1')['wallet_balance'],100)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM wisp_voucher_batches')['n'],1)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM wisp_vouchers')['n'],6)
    def test_generation_radius_failure_rolls_back_everything(self):
        cls=self.ns['_VoucherWriter'];many=cls.many
        def fail(tx,statement,params):
            if 'INSERT INTO radusergroup' in statement:raise RuntimeError('fixture RADIUS failure')
            return many(tx,statement,params)
        with patch.object(cls,'many',fail),self.assertRaises(RuntimeError):self.ns['generate_voucher_batch']('fixture',1,2,reseller_id=1)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM wisp_vouchers')['n'],6)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM wisp_manager_invoices')['n'],0)
    def test_generation_invalid_reseller_rolls_back(self):
        with self.assertRaises(ValueError):self.ns['generate_voucher_batch']('fixture',1,1,reseller_id=999)
        self.assertEqual(self.row('SELECT COUNT(*) n FROM wisp_voucher_batches')['n'],1)
    def test_generation_retries_existing_username(self):
        values=iter(['fixture-1','fixture-new'])
        with patch.dict(self.ns,generate_random_code=lambda *a:next(values)):
            ident,_=self.ns['generate_voucher_batch']('fixture',1,1)
        self.assertEqual(self.row('SELECT username FROM wisp_vouchers WHERE batch_id=?',(ident,))['username'],'fixture-new')
    def test_generation_zero_cost_and_three_credential_modes(self):
        for pin,same in [(True,False),(False,True),(False,False)]:
            ident,_=self.ns['generate_voucher_batch']('fixture',1,2,reseller_id=1,pin_only=pin,same_user_pass=same)
            rows=self.db.execute('SELECT username,password,status,snap_validity_value FROM wisp_vouchers WHERE batch_id=?',(ident,)).fetchall()
            self.assertEqual(len(rows),2)
            for row in rows:
                self.assertEqual(row['status'],'unused');self.assertEqual(row['snap_validity_value'],0)
                self.assertEqual(row['username']==row['password'],pin or same)
        self.assertEqual(self.row('SELECT wallet_balance FROM wisp_managers WHERE id=1')['wallet_balance'],100)
    def test_profile_radius_failure_rolls_back_password(self):
        sync=self.ns['sync_voucher_to_radius']
        def fail(*args,**kwargs):sync(*args,**kwargs);raise RuntimeError('fixture sync failure')
        with patch.dict(self.ns,sync_voucher_to_radius=fail),self.assertRaises(RuntimeError):self.ns['update_voucher_card'](2,'fixture-2','fixture-changed')
        self.assertEqual(self.row('SELECT password FROM wisp_vouchers WHERE id=2')['password'],'fixture-password')
        self.assertEqual(self.row("SELECT value FROM radcheck WHERE username='fixture-2' AND attribute='Cleartext-Password'")['value'],'fixture-password')
    def test_profile_mac_sync_and_manual_suspension(self):
        self.ns['update_voucher_card'](2,'fixture-2','fixture-changed',bound_mac='aa:bb:cc:dd:ee:ff')
        self.assertEqual(self.row("SELECT value FROM radcheck WHERE username='fixture-2' AND attribute='Calling-Station-Id'")['value'],'AA:BB:CC:DD:EE:FF')
        self.db.execute("UPDATE wisp_vouchers SET status='suspended' WHERE id=2");self.db.commit()
        self.ns['update_voucher_card'](2,'fixture-2','fixture-next',bound_mac='')
        self.assertEqual(self.row("SELECT COUNT(*) n FROM radcheck WHERE username='fixture-2'")['n'],0)
    def test_used_rename_rejected_but_unused_rename_allowed(self):
        with self.assertRaises(ValueError):self.ns['update_voucher_card'](2,'fixture-renamed','fixture-pass')
        self.ns['update_voucher_card'](1,'fixture-renamed','fixture-pass')
        self.assertEqual(self.row('SELECT username FROM wisp_vouchers WHERE id=1')['username'],'fixture-renamed')
    def test_profile_does_not_change_lifecycle(self):
        r=self.post('/vouchers/update-card',data={'card_id':2,'status':'suspended','bound_mac':''})
        self.assertEqual(r.status_code,302);self.assertEqual(self.row('SELECT status FROM wisp_vouchers WHERE id=2')['status'],'active')
    def test_combined_topup_preserves_suspension_snapshot_and_cycle(self):
        self.db.execute("UPDATE wisp_vouchers SET status='suspended',pause_reason='fixture pause' WHERE id=2");self.db.commit()
        ok,_=self.ns['action_adjust_entitlement']('voucher',2,days=1,quota_amount=2,quota_unit='MB')
        self.assertTrue(ok)
        row=self.row('SELECT * FROM wisp_vouchers WHERE id=2')
        self.assertEqual((row['status'],row['pause_reason'],row['snap_volume_quota_mb'],row['extra_quota_mb'],row['last_renewed_at']),('suspended','fixture pause',10,7,'2026-10-11 17:00:00'))
        self.assertEqual(self.row("SELECT COUNT(*) n FROM radcheck WHERE username='fixture-2'")['n'],0)
    def test_combined_topup_radius_failure_rolls_back_both_parts(self):
        self.stubs['core.radius_sync'].sync_voucher_to_radius=lambda *a,**k:False
        with self.assertRaises(RuntimeError):self.ns['action_adjust_entitlement']('voucher',2,days=1,quota_amount=2)
        row=self.row('SELECT * FROM wisp_vouchers WHERE id=2');self.assertEqual(row['extra_quota_mb'],5);self.assertEqual(row['expires_at'],'2027-01-01 12:00:00')
    def test_zero_nonfinite_and_consumed_topup_rejected(self):
        for ident,days,quota in [(2,0,0),(2,-1,0),(2,0,'NaN'),(4,1,2)]:
            self.assertFalse(self.ns['action_adjust_entitlement']('voucher',ident,days=days,quota_amount=quota)[0])
    def test_signed_quota_preserves_active_cycle(self):
        self.assertTrue(self.ns['action_add_quota']('voucher',2,-2,'MB')[0])
        row=self.row('SELECT * FROM wisp_vouchers WHERE id=2');self.assertEqual(row['extra_quota_mb'],3);self.assertEqual(row['status'],'active')
    def test_expired_topup_reactivates_only_when_all_limits_allow(self):
        self.db.execute('UPDATE wisp_vouchers SET snap_volume_quota_mb=10 WHERE id=3');self.db.commit()
        self.assertTrue(self.ns['action_add_quota']('voucher',3,1,'MB')[0])
        self.assertEqual(self.row('SELECT status FROM wisp_vouchers WHERE id=3')['status'],'active')
        self.db.execute("UPDATE wisp_vouchers SET status='expired',expires_at='2000-01-01 12:00:00' WHERE id=3");self.db.commit()
        self.assertTrue(self.ns['action_add_quota']('voucher',3,1,'MB')[0])
        self.assertEqual(self.row('SELECT status FROM wisp_vouchers WHERE id=3')['status'],'expired')
    def test_crossing_cycle_and_missing_baseline(self):
        self.seed('wisp_session_baselines',radacctid=1,username='fixture-2',baseline_input_bytes=25,baseline_output_bytes=50,baseline_seconds=100,renewed_at='2026-10-11 17:00:00')
        card=self.context('/vouchers/active-users?status=active','active_card_users')['active_cards'][0]
        self.assertEqual(card['used_bytes_str'],'225 B');self.assertFalse(card['accounting_unavailable'])
        self.db.execute('DELETE FROM wisp_session_baselines');self.db.commit()
        card=self.context('/vouchers/active-users?status=active','active_card_users')['active_cards'][0]
        self.assertTrue(card['accounting_unavailable']);self.assertEqual(card['used_bytes_str'],'غير متاح')
    def test_unlimited_quota_survives_extra_and_online_pill_matches(self):
        self.db.execute('UPDATE wisp_vouchers SET snap_volume_quota_mb=0 WHERE id=2');self.db.commit()
        context=self.context('/vouchers/active-users?status=online','active_card_users');card=context['active_cards'][0]
        self.assertFalse(card['has_quota']);self.assertTrue(card['is_online']);self.assertEqual(context['status_counts']['online'],1)
    def test_inspection_accepts_legacy_link_and_cycle_totals(self):
        context=self.context('/vouchers/inspect?code=fixture-2','card_inspect')
        self.assertEqual(context['card']['id'],2);self.assertEqual(context['diag']['total_traffic_bytes'],225);self.assertEqual(context['diag']['total_quota_str'],'15728640 B')
    def test_unknown_accounting_renders_without_invented_zero(self):
        self.stubs['services.quota_service'].calculate_account_quota=lambda *a,**k:dict(accounting_error=True)
        for path in ['/vouchers/active-users/2','/vouchers/inspect?q=fixture-2']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200);self.assertIn('غير متاح',response.get_data(as_text=True))
    def test_card_disconnect_resolves_voucher_and_never_subscriber(self):
        calls=[];self.ns['action_disconnect_user']=lambda *a,**k:(calls.append(a) or True,'fixture queued, not Stop')
        r=self.post('/api/vouchers/disconnect',json={'username':'fixture-2'});self.assertTrue(r.json['success']);self.assertEqual(calls,[('voucher',2)])
        r=self.post('/api/vouchers/disconnect',json={'username':'fixture-missing'});self.assertEqual(r.status_code,404);self.assertEqual(len(calls),1)
    def test_bulk_failure_and_duplicate_ids(self):
        self.ns['delete_batch']=lambda *a,**k:False
        r=self.post('/vouchers/batches/bulk-action',json={'action':'delete','batch_ids':[1]});self.assertFalse(r.json['success']);self.assertEqual(r.json['failed'],1)
        calls=[];self.ns['update_voucher_batch']=lambda ident,**k:calls.append(ident)
        r=self.post('/vouchers/batches/bulk-action',json={'action':'change_package','batch_ids':[1,'01',1],'package_id':1});self.assertEqual(calls,[1]);self.assertEqual(r.json['successful'],1)
        self.assertEqual(self.post('/vouchers/batches/bulk-action',json={'action':'delete','batch_ids':[1,'bad']}).status_code,400)
    def test_reseller_assignment_failure_rolls_back_both_tables(self):
        db_stub=types.ModuleType('database.db');db_stub.db_session=self.ns['db_session']
        service=types.ModuleType('services.voucher_service');service._VoucherWriter=self.ns['_VoucherWriter']
        cls=self.ns['_VoucherWriter'];write=cls.write
        def fail(tx,statement,params=()):
            if 'UPDATE wisp_vouchers' in statement:raise RuntimeError('fixture assignment failure')
            return write(tx,statement,params)
        with patch.dict('sys.modules',{'database.db':db_stub,'services.voucher_service':service}),patch.object(cls,'write',fail),self.assertLogs('audit',level='ERROR'):
            result=self.post('/vouchers/batches/bulk-action',json={'action':'assign_reseller','reseller_id':1,'batch_ids':[1]})
        self.assertFalse(result.json['success'])
        self.assertIsNone(self.row('SELECT reseller_id FROM wisp_voucher_batches WHERE id=1')['reseller_id'])
        self.assertIsNone(self.row('SELECT reseller_id FROM wisp_vouchers WHERE id=1')['reseller_id'])
    def test_print_zero_values_and_qr_roundtrip(self):
        self.db.execute('UPDATE wisp_vouchers SET username=?,password=?,snap_price=0 WHERE id=1',('fixture&part','fixture+part'));self.db.commit()
        _,cards,_=self.ns['get_batch_cards_for_print'](1);card=cards[0]
        self.assertEqual(card['validity'],'غير محدود');self.assertEqual(card['price'],0)
        self.assertEqual(parse_qs(urlsplit(card['login_url']).query),dict(username=['fixture&part'],password=['fixture+part']))
        html=self.client.get('/vouchers/print/1').get_data(as_text=True)
        self.assertNotIn('text: "http://wifi.hotspot/login?username=fixture-1&amp;',html)
    def test_existing_design_without_background_and_clean_design(self):
        for mode in ['image','clean']:
            result=self.ns['save_card_design']({'id':1,'name':'fixture edited','design_type':mode,'config':{}});self.assertTrue(result['success'])
    def test_quoted_names_keep_all_inline_handlers_valid(self):
        weird="fixture'\" & <tag>"
        self.db.execute('UPDATE wisp_vouchers SET username=?,password=? WHERE id=2',(weird,weird));self.db.execute('UPDATE wisp_voucher_batches SET name=? WHERE id=1',(weird,));self.db.execute('UPDATE wisp_card_templates SET name=? WHERE id=1',(weird,));self.db.commit()
        paths=['/vouchers','/vouchers/batch/1','/vouchers/active-users?status=all','/vouchers/active-users/2','/vouchers/inspect?q=fixture%27%22%20%26%20%3Ctag%3E','/vouchers/designs']
        with tempfile.TemporaryDirectory() as temp:
            for path in paths:
                response=self.client.get(path);self.assertEqual(response.status_code,200,path)
                handlers=[attrs['onclick'] for _,attrs in Elements(response.get_data(as_text=True)).items if 'onclick' in attrs]
                script=Path(temp)/'events.js';script.write_text('\n'.join('function h'+str(i)+'(event){'+code+'}' for i,code in enumerate(handlers)),encoding='utf-8')
                result=subprocess.run(['node','--check',str(script)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
    def test_suspended_card_action_is_activation(self):
        html=self.client.get('/vouchers/active-users?status=suspended').get_data(as_text=True)
        self.assertIn('activateVoucher(5,',html);self.assertIn('تنشيط الحساب',html);self.assertIn('fa-user-check',html)
    def test_activation_selects_active_without_renewal(self):
        before=self.row('SELECT * FROM wisp_vouchers WHERE id=5')
        result=self.post('/api/vouchers/activate',json={'card_id':5})
        self.assertTrue(result.json['success']);self.assertEqual(result.json['status'],'active')
        after=self.row('SELECT * FROM wisp_vouchers WHERE id=5')
        for key in ['expires_at','last_renewed_at','balance','extra_quota_mb','snap_volume_quota_mb']:
            self.assertEqual(before[key],after[key])
    def test_activation_expired_time_selects_expired(self):
        self.db.execute("UPDATE wisp_vouchers SET expires_at='2000-01-01 12:00:00' WHERE id=5");self.db.commit()
        result=self.post('/api/vouchers/activate',json={'card_id':5})
        self.assertTrue(result.json['success']);self.assertEqual(result.json['status'],'expired')
        self.assertEqual(self.row("SELECT COUNT(*) n FROM radcheck WHERE username='fixture-5'")['n'],0)
    def test_activation_missing_baseline_rejects_without_changes(self):
        self.db.execute("UPDATE wisp_vouchers SET status='suspended' WHERE id=2");self.db.commit()
        result=self.post('/api/vouchers/activate',json={'card_id':2})
        self.assertEqual(result.status_code,400);self.assertFalse(result.json['success'])
        self.assertEqual(self.row('SELECT status FROM wisp_vouchers WHERE id=2')['status'],'suspended')
    def test_consumed_card_never_receives_credentials_on_reset(self):
        self.assertTrue(self.post('/api/vouchers/reset-mac',json={'username':'fixture-4'}).json['success'])
        self.assertEqual(self.row("SELECT COUNT(*) n FROM radcheck WHERE username='fixture-4'")['n'],0)

if __name__=='__main__':unittest.main()
