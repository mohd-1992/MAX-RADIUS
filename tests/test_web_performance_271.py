"""Web read optimizations: no app, workers, financial writes or network."""
import ast
import datetime as dt
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from services import report_catalog_cache as cache
from services.card_list_page_service import page_ids, remaining_seconds
from services.list_sort_service import sort_rows
from services.sales_reporting_service import LEDGER, report_source

ROOT=Path(__file__).resolve().parents[1]


class CatalogConcurrency(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.temp=tempfile.TemporaryDirectory()
        helper=types.ModuleType('services.heavy_operation_service')
        helper.lock_root=lambda:Path(self.temp.name)
        self.patch=patch.dict('sys.modules',{'services.heavy_operation_service':helper})
        self.patch.start()

    def tearDown(self):
        self.patch.stop();self.temp.cleanup();cache.clear()

    def test_one_source_read_for_concurrent_misses_and_no_shared_mutability(self):
        calls=[];start=threading.Barrier(8)
        def source(sql,params=()):
            calls.append(1);time.sleep(.08);return [{'option':['value']}]
        reader=cache.catalog_query(source,None)
        def task(_):start.wait();return reader('SELECT dimensions')
        with ThreadPoolExecutor(8) as pool:results=list(pool.map(task,range(8)))
        self.assertEqual(len(calls),1)
        results[0][0]['option'].append('mutated')
        self.assertEqual(reader('SELECT dimensions'),[{'option':['value']}])
        self.assertEqual(cache._pending,{})

    def test_restricted_scopes_never_share_global_cache(self):
        calls=[]
        def query(sql,params=()):calls.append(1);return [{'n':len(calls)}]
        self.assertNotEqual(cache.catalog_query(query,1)('same'),cache.catalog_query(query,2)('same'))
        self.assertEqual(len(cache._cache),0)

    def test_failure_releases_waiter_and_does_not_cache_an_error(self):
        def failed(*a):raise RuntimeError('read failed')
        with self.assertRaises(RuntimeError):cache.catalog_query(failed,None)('same')
        self.assertEqual(cache._pending,{})
        self.assertEqual(cache.catalog_query(lambda *a:[{'ok':True}],None)('same'),[{'ok':True}])

    def test_clear_cannot_republish_previous_generation_to_new_requests(self):
        entered=threading.Event();release=threading.Event()
        def source(*a):entered.set();release.wait(2);return [{'v':'old'}]
        with ThreadPoolExecutor(1) as pool:
            job=pool.submit(cache.catalog_query(source,None),'same')
            self.assertTrue(entered.wait(1));cache.clear();release.set();job.result()
        self.assertEqual(cache.catalog_query(lambda *a:[{'v':'new'}],None)('same'),[{'v':'new'}])

    def test_capacity_and_large_result_bounds_remain(self):
        query=cache.catalog_query(lambda *a:[{'v':1}],None)
        for i in range(20):query(str(i))
        self.assertEqual(len(cache._cache),16)
        cache.clear();cache.catalog_query(lambda *a:[{}]*10001,None)('large')
        self.assertEqual(len(cache._cache),0)


class CompactKeys(unittest.TestCase):
    now=dt.datetime(2026,11,6,15)

    def test_days_preserve_unknown_expired_boundary_units_and_ties(self):
        rows=[dict(id=i,username=str(i),status='active',expires_at=None,first_used_at=None,
                   validity_value=value,validity_unit=unit)
              for i,(value,unit) in enumerate([(2,'days'),(48,'hours'),('bad','days'),(0,'days'),('١','days')],1)]
        rows += [dict(id=6,status='expired',expires_at=self.now+dt.timedelta(days=5)),
                 dict(id=7,status='active',expires_at=self.now),
                 dict(id=8,status='active',expires_at='bad',validity_value=3,validity_unit='minutes')]
        for direction in ('asc','desc'):
            state=dict(key='days',direction=direction,table='cards')
            expected=[dict(r,sort_remaining_seconds=remaining_seconds(r,self.now)) for r in rows]
            def query(sql,params=()):
                self.assertNotIn('v.*',sql);self.assertEqual(params,('scope',));return [dict(r) for r in rows]
            for offset in (0,2,7,100):
                ids,usage=page_ids(query,'v.status=?',('scope',),state,offset,3,self.now,'cutoff',lambda _:self.fail('days must not read quota'),RuntimeError)
                self.assertEqual(ids,[r['id'] for r in sort_rows(expected,state)[offset:offset+3]])
                self.assertIsNone(usage)

    def test_consumption_keeps_unknown_last_and_reuses_original_usage(self):
        rows=[dict(id=i,username=str(i)) for i in range(1,5)]
        usage={'1':dict(down_bytes=10,up_bytes=20),'2':None,'3':dict(down_bytes=30,up_bytes=0)}
        ids,original=page_ids(lambda *a:rows,'1=1',(),dict(key='consumption',direction='desc',table='cards'),0,4,self.now,'cutoff',lambda _:usage,RuntimeError)
        self.assertEqual(ids,[3,1,4,2]);self.assertIs(original,usage)

    def test_failed_consumption_read_is_unknown_for_every_account(self):
        def failed(_):raise RuntimeError('unavailable')
        ids,usage=page_ids(lambda *a:[dict(id=i,username=str(i)) for i in (1,2)],'1=1',(),dict(key='consumption',direction='asc',table='cards'),0,50,self.now,'cutoff',failed,RuntimeError)
        self.assertEqual(ids,[2,1]);self.assertEqual(usage,{})

    def test_empty_scope_never_reads_sessions_or_quota(self):
        self.assertEqual(page_ids(lambda *a:[],'1=0',(),dict(key='days'),0,50,self.now,'cutoff',None,RuntimeError),([],None))

    def test_session_sort_keys_keep_online_last_seen_and_missing_values(self):
        rows=[dict(id=i,username=f'u{i}',first_used_at=None) for i in range(1,5)]
        rows[2]['first_used_at']=dt.datetime(2026,11,3)
        active=[dict(username='u1',framedipaddress='192.0.2.8',calledstationid='HS',nasipaddress='192.0.2.1')]
        previous=[dict(username='u2',acctstoptime=dt.datetime(2026,11,2,1,2),framedipaddress='192.0.2.9',calledstationid='',nasipaddress='192.0.2.1')]
        expected=[dict(rows[0],is_online=True,ip_address='192.0.2.8',access_point='HS',last_seen_str='متصل الآن'),
            dict(rows[1],is_online=False,ip_address='192.0.2.9',access_point='192.0.2.1',last_seen_str='2026-11-02 01:02'),
            dict(rows[2],is_online=False,ip_address='-',access_point='-',last_seen_str='2026-11-03 00:00'),
            dict(rows[3],is_online=False,ip_address='-',access_point='-',last_seen_str='لم يتصل بعد')]
        for key in ('ip','accesspoint','lastseen'):
            for direction in ('asc','desc'):
                def query(sql,params=()):
                    if 'SELECT v.id' in sql:return [dict(r) for r in rows]
                    if 'INNER JOIN' in sql:return previous
                    return active
                state=dict(key=key,direction=direction,table='cards')
                ids,usage=page_ids(query,'1=1',(),state,1,2,self.now,'cutoff',lambda _:self.fail('session sort must not read quota'),RuntimeError)
                self.assertEqual(ids,[r['id'] for r in sort_rows(expected,state)[1:3]])


class SourceBoundaries(unittest.TestCase):
    def test_each_report_source_is_exact_original_branch(self):
        voucher,invoice=LEDGER[1:-3].split('    UNION ALL\n',1)
        self.assertTrue(report_source('voucher').startswith('('+voucher+') e LEFT JOIN'))
        for kind in ('voucher','invoice'):self.assertNotIn('UNION ALL',report_source(kind))
        for field in ('event_kind','event_at','username','serial_number','batch_name','package_name','price','cost','reseller_id','operation'):
            self.assertIn('AS '+field,report_source('invoice'))
        self.assertIn("WHERE i.status='paid'",report_source('invoice'))
        with self.assertRaises(ValueError):report_source("voucher' OR 1=1")

    def test_no_paid_write_or_app_import_in_compact_selector(self):
        tree=ast.parse((ROOT/'services/card_list_page_service.py').read_text())
        for n in ast.walk(tree):
            if isinstance(n,ast.Name):self.assertNotIn(n.id,('execute_write','execute_update','commit','activate_voucher_card'))
        source=(ROOT/'services/report_catalog_cache.py').read_text()
        self.assertIn('pending.wait(10)',source)
        self.assertIn('len(_pending)<16',source)

    def test_excel_indexed_join_only_for_the_proven_collation_pair(self):
        import services.batch_export_service as exports
        for collations in [('utf8mb4_unicode_ci','utf8mb4_unicode_ci'),
                           ('utf8mb4_bin','utf8mb4_bin'),
                           ('utf8mb4_general_ci','utf8mb4_unicode_ci'),(None,None)]:
            statements=[]
            class Cursor:
                def execute(self,sql,args=()):
                    statements.append(sql)
                    if sql.startswith('SHOW FULL'):
                        self.rows=[{'Collation':collations[0 if 'radacct' in sql else 1]}]
                    elif 'SELECT * FROM wisp_voucher_batches' in sql:self.rows=[dict(id=1,reseller_id=None)]
                    elif 'SELECT v.*' in sql:self.rows=[dict(id=1,username='X',status='unused')]
                    elif 'SELECT LOWER(s.username)' in sql:self.rows=[dict(username='x',up_bytes=0,down_bytes=0,uptime_secs=0,incomplete=0)]
                    else:self.rows=[]
                    self.description=[(k,) for k in self.rows[0]] if self.rows else []
                def fetchall(self):return self.rows
                def close(self):pass
            class Connection:
                def cursor(self):return Cursor()
                def rollback(self):pass
                def close(self):pass
            stub=types.ModuleType('database.db');stub.get_connection=Connection
            stub.is_mysql_conn=lambda c:True;stub.adapt_query=lambda sql,c:sql
            with patch.dict('sys.modules',{'database.db':stub}),patch.object(exports,'export_rate_limit',return_value='0/0'):
                exports.export_snapshot(1)
            aggregation=next(sql for sql in statements if 'SELECT LOWER(s.username)' in sql)
            if collations==('utf8mb4_unicode_ci','utf8mb4_unicode_ci'):
                self.assertIn('ON a.username=s.username',aggregation)
            else:self.assertIn('ON LOWER(a.username)=LOWER(s.username)',aggregation)
            self.assertFalse(any(s.lstrip().upper().startswith(('UPDATE','INSERT','DELETE')) for s in statements))


if __name__=='__main__':unittest.main()
