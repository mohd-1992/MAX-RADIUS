"""List accounting, bulk IDs and browser script regressions; isolated data only."""
import ast
import datetime
import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from flask import render_template, session
from test_subscriber_admin_safety import fixture
from test_package_page import ROOT, load_functions

class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__();self.items=[];self.feed(html)
    def handle_starttag(self, tag, attrs):self.items.append((tag,dict(attrs)))

class ListSafetyTests(unittest.TestCase):
    def setUp(self):
        self.app,self.db,self.ns=fixture()
        self.db.execute('ALTER TABLE wisp_subscribers ADD COLUMN created_at TEXT')
        self.db.execute('ALTER TABLE wisp_subscribers ADD COLUMN first_used_at TEXT')
        self.db.execute('ALTER TABLE radacct ADD COLUMN calledstationid TEXT')
        self.db.execute('CREATE TABLE wisp_session_baselines(radacctid INTEGER,renewed_at TEXT,baseline_input_bytes INTEGER,baseline_output_bytes INTEGER,baseline_seconds INTEGER)')
        self.db.execute("UPDATE wisp_subscribers SET last_renewed_at='2026-10-11 17:00:00',created_at='2026-10-01 12:00:00'")
        class AccountingReadError(Exception):pass
        class IncompleteCycleAccountingError(AccountingReadError):pass
        self.ns.update(AccountingReadError=AccountingReadError,IncompleteCycleAccountingError=IncompleteCycleAccountingError,
            adapt_query=lambda q,c:q,is_mysql_conn=lambda c:False,
            get_heartbeat_cutoff_str=lambda n:'2026-10-11 17:55:00')
        load_functions('services/quota_service.py',['get_accounting_totals_batch','get_subscriber_accounting_totals_batch','get_accounting_totals'],self.ns)
        # Compare the canonical DB aggregation, outside financial live-preview context.
        tree=ast.parse((ROOT/'services/quota_service.py').read_text(encoding='utf-8'))
        original=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_accounting_totals')
        exec(compile(ast.Module(body=[original],type_ignores=[]),'quota-original','exec'),self.ns)
        load_functions('services/subscriber_service.py',['get_subscribers'],self.ns)
    def tearDown(self):self.db.close()
    def add_session(self,ident=1,start='2026-10-11 16:00:00',stop=None,up=100,down=200):
        self.db.execute('INSERT INTO radacct(radacctid,username,acctsessionid,acctstarttime,acctupdatetime,acctstoptime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(?,?,?,?,?,?,?,?,?)',
            (ident,'fixture-user',str(ident),start,'2026-10-11 18:00:00',stop,up,down,7200))
    def baseline(self,ident=1,up=25,down=50):
        self.db.execute('INSERT INTO wisp_session_baselines VALUES(?,?,?,?,?)',(ident,'2026-10-11 17:00:00',up,down,3600))
    def listed(self):return self.ns['get_subscribers'](status='all')[0]
    def test_crossing_cycle_uses_baseline_and_matches_authoritative_totals(self):
        self.add_session();self.baseline()
        self.add_session(2,start='2026-10-11 17:30:00',up=10,down=20)
        batch=self.ns['get_subscriber_accounting_totals_batch']([1])['fixture-user']
        official=self.ns['get_accounting_totals']('fixture-user','2026-10-11 17:00:00',conn=self.db)
        for key in ['up_bytes','down_bytes','total_bytes','uptime_secs']:self.assertEqual(batch[key],official[key])
        self.assertEqual(self.listed()['used_bytes_str'],'255 B')
    def test_missing_baseline_is_unknown_and_not_zero(self):
        self.add_session()
        row=self.listed();self.assertFalse(row['accounting_available']);self.assertEqual(row['used_bytes_str'],'غير متاح')
        self.assertIsNone(row['consumption_percent'])
    def test_closed_previous_cycle_excluded_counter_reset_clamped(self):
        self.add_session(stop='2026-10-11 16:30:00',up=9999,down=9999)
        self.add_session(2);self.baseline(2,up=150,down=250)
        self.assertEqual(self.listed()['used_bytes_str'],'0 B')
    def test_no_sessions_zero_but_unlimited_with_extra_stays_unlimited(self):
        self.db.execute('UPDATE wisp_subscribers SET snap_volume_quota_mb=0,extra_quota_mb=50')
        row=self.listed();self.assertTrue(row['accounting_available']);self.assertFalse(row['has_quota'])
        self.assertEqual(row['quota_bytes_str'],'غير محدود');self.assertEqual(row['used_bytes_str'],'0 B')
    def test_first_activation_precedes_creation_fallback_when_never_renewed(self):
        self.db.execute("UPDATE wisp_subscribers SET last_renewed_at=NULL,first_used_at='2026-10-11 17:00:00'")
        self.add_session(stop='2026-10-11 16:30:00',up=9999,down=9999)
        self.add_session(2,start='2026-10-11 17:30:00',up=10,down=20)
        self.assertEqual(self.listed()['used_bytes_str'],'30 B')
    def test_large_octets_are_not_counted_twice(self):
        self.add_session(start='2026-10-11 17:30:00',up=2**32+5,down=2**32+7)
        self.assertEqual(self.listed()['used_bytes_str'],str(2**33+12)+' B')
    def test_failed_baseline_query_is_unavailable(self):
        self.db.execute('DROP TABLE wisp_session_baselines')
        self.assertFalse(self.listed()['accounting_available'])
    def test_batch_chunks_without_per_account_queries(self):
        calls=[]
        self.ns['query_all']=lambda sql,params:(calls.append(params) or [])
        self.ns['get_subscriber_accounting_totals_batch'](range(1,902))
        self.assertEqual([len(c) for c in calls],[400,400,101])
    def test_bulk_equivalent_ids_execute_once_in_order(self):
        for ids in [[1,'1','01','2',2],'[1,"1","01",2]','1,1,01,2']:
            calls=[]
            self.assertTrue(self.ns['action_bulk_execute'](lambda kind,ident:(calls.append(ident) or True,'ok'),'subscriber',ids)[0])
            self.assertEqual(calls,[1,2])
    def test_bulk_invalid_entire_batch_rejected_before_first_effect(self):
        for bad in [None,True,1.5,'-1','0','abc','',{},['1'],'١']:
            calls=[]
            result=self.ns['action_bulk_execute'](lambda *a:(calls.append(a) or True,'ok'),'subscriber',[1,bad])
            self.assertFalse(result[0]);self.assertEqual(calls,[])
        self.assertFalse(self.ns['action_bulk_execute']('unknown','subscriber',[1])[0])
    def test_bulk_wallet_duplicates_produce_one_invoice(self):
        self.assertTrue(self.ns['action_bulk_execute'](self.ns['action_add_wallet_balance'],'subscriber',[1,'01','1'],amount=10)[0])
        self.assertEqual(self.db.execute('SELECT balance FROM wisp_subscribers').fetchone()[0],110)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wisp_invoices').fetchone()[0],1)
    def render(self,name,**context):
        with self.app.test_request_context('/subscribers'):
            session['permissions']=['subscribers.view','subscribers.edit','subscribers.disconnect','subscribers.renew','subscribers.delete','wallets.manage']
            return render_template(name,**context)
    def test_quoted_account_and_package_values_survive_inline_actions(self):
        weird="O'Reilly\"\\\n<svg/onload=bad()>"
        self.db.execute('UPDATE wisp_subscribers SET username=?,password=?',(weird,weird))
        row=self.listed();row['package_name']=weird
        list_html=self.render('subscribers.html',subscribers=[row],packages=[],status_counts={},current_status='all')
        detail_html=self.render('subscriber_details.html',subscriber=row,sessions=[],invoices=[],audit_logs=[],analytics=dict(has_data=False),analytics_json='{}',usage_percent=0)
        handlers=[]
        names=['confirmDelete','openExtendModal','confirmTerminate','confirmRenew','openChangePackageModal','openAddQuotaModal','openUsageHistoryModal','openAddWalletModal','openDeductWalletModal','confirmDisconnect','disconnectUser','togglePasswordVisibility','copyToClipboard']
        for html in [list_html,detail_html]:
            for tag,attrs in Elements(html).items:
                h=attrs.get('onclick','')
                if any(h.startswith(name+'(') for name in names):handlers.append(h)
        self.assertGreaterEqual(len(handlers),12)
        code="const calls=[];const names="+json.dumps(names)+";for(const n of names)global[n]=(...args)=>calls.push(args);for(const h of "+json.dumps(handlers)+")new Function(h)();if(!calls.every(args=>args.includes("+json.dumps(weird)+")))throw Error('value changed');"
        check=subprocess.run(['node','-e',code],capture_output=True,text=True)
        self.assertEqual(check.returncode,0,check.stderr)
    def test_detail_validity_zero_and_units(self):
        row=self.listed()
        for val,unit,text in [(0,'days','غير محدودة'),(2,'hours','2 ساعة'),(3,'minutes','3 دقيقة'),(1,'months','1 شهر')]:
            row.update(validity_value=val,validity_unit=unit)
            html=self.render('subscriber_details.html',subscriber=row,sessions=[],invoices=[],audit_logs=[],analytics=dict(has_data=False),analytics_json='{}',usage_percent=0)
            self.assertIn(text,html)
    def test_create_preserves_zero_over_legacy_days(self):
        self.db.execute('UPDATE wisp_packages SET validity_value=0,validity_days=30')
        client=self.app.test_client()
        with client.session_transaction() as s:
            s['manager']={'id':2,'role_id':2};s['permissions']=['subscribers.create']
        response=client.get('/subscribers/create');self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        options=[attrs for tag,attrs in Elements(html).items if tag=='option' and 'data-val' in attrs]
        self.assertTrue(options);self.assertTrue(all(p['data-val']=='0' for p in options))
    def test_package_preview_text_and_zero_validity_execute_in_javascript(self):
        source=(ROOT/'web/templates/subscriber_create.html').read_text(encoding='utf-8')
        function=source[source.index('    function handlePackageChange'):source.index('</script>',source.index('    function handlePackageChange'))]
        function=function.replace('{{ currency_symbol }}','عملة')
        script='''const vm=require('vm');const assert=require('assert');
            const elements={}; const document={getElementById:id=>elements[id]||(elements[id]={classList:{remove:()=>{}},set innerHTML(v){throw Error('HTML interpolation')}})};
            const context={document};vm.createContext(context);
        '''+'vm.runInContext('+json.dumps(function)+''',context);
            const attributes={'data-val':'0','data-unit':'days','data-quota':'0'};
            const option={value:'1',text:'<svg/onload=bad()>',getAttribute:n=>attributes[n]};
            const select={options:[option],selectedIndex:0};context.handlePackageChange(select);
            assert.equal(elements.pkgCardNameText.textContent,option.text);
            assert.equal(elements.pkgCardValidity.innerText,'غير محدودة');
            for(const [value,unit,expected] of [[2,'hours','2 ساعة'],[3,'minutes','3 دقيقة'],[4,'days','4 يوم'],[1,'months','1 شهر']]){
                attributes['data-val']=String(value);attributes['data-unit']=unit;context.handlePackageChange(select);
                assert.equal(elements.pkgCardValidity.innerText,expected);
            }
        '''
        check=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(check.returncode,0,check.stderr)

if __name__=='__main__':unittest.main()
