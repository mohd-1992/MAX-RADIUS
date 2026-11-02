"""Read-only MariaDB parity for compact consumption and six-month sales.
Explicit existing isolated fixture only; no schema, seed, worker or NAS writes.
"""
import os, sys, datetime as dt, unittest
from unittest.mock import patch
assert sys.argv[1:] == ['--run'] and os.environ.get('DB_NAME') in ('sales266_fixture','reports_audit')
from database.db import get_connection
from services import dashboard_reporting_service as dash
from services.traffic_sample_service import series
from services.report_time_axis import sales_series
NOW=dt.datetime(2026,10,27,12,tzinfo=dt.timezone(dt.timedelta(hours=3)))

def literal(v):
    return 'NULL' if v is None else str(v) if isinstance(v,int) else "'"+str(v).replace("'","''")+"'"

def relation(rows):
    return '('+' UNION ALL '.join('SELECT '+','.join(literal(v)+' AS '+k for k,v in r.items()) for r in rows)+')'

base=dict(bucket_at='2026-10-27 01:00:00',owner_scope=7,point_key='point-a',nas_address='192.0.2.1',service_type='hotspot',point_known=1,download_bytes=300,upload_bytes=30,download_known=1,upload_known=1,gaps=0,observations=1)
rows=[base,dict(base,bucket_at='2026-10-26 23:45:00',download_bytes=100,upload_bytes=10),
      dict(base,owner_scope=8,point_key='point-b',nas_address='192.0.2.2',download_bytes=200,upload_bytes=20),
      dict(base,bucket_at='2026-10-27 02:00:00',download_known=0,upload_known=0,gaps=2)]
rollups=relation(rows)
ledger="(SELECT 'voucher' event_kind,7 reseller_id,CAST('2026-05-01 02:00:00' AS DATETIME) event_at,20 price UNION ALL SELECT 'voucher',7,'2026-10-26 02:00:00',100 UNION ALL SELECT 'invoice',NULL,'2026-10-26 03:00:00',50 UNION ALL SELECT 'voucher',8,'2026-10-27 01:00:00',25) e"

def query(sql,params=()):
    if 's.epoch' in sql:return [dict(epoch='virtual-consistent-backup',marker='virtual-consistent-backup')]
    sql=sql.replace('FROM wisp_traffic_rollups','FROM '+rollups+' virtual_rollups')
    with get_connection() as conn:
        with conn.cursor() as cur:cur.execute(sql,params);return list(cur.fetchall())

def read(reader,**kwargs):return reader(query,**kwargs)

class CompactParity(unittest.TestCase):
    def test_each_day_matches_report(self):
        for timezone in ('Asia/Aden','Asia/Kathmandu','America/New_York'):
            daily=dash.traffic_series(query,now=NOW,timezone=timezone)
            weekly=series(query,dict(period='this_week'),now=NOW,timezone=timezone)
            self.assertEqual(len(daily),7)
            for row,down,up in zip(daily,weekly['download'],weekly['upload']):
                self.assertEqual(row['download'],None if down is None else round(down*1073741824))
                self.assertEqual(row['upload'],None if up is None else round(up*1073741824))

    def test_hourly_total_and_missing_period(self):
        chart=series(query,dict(period='today'),now=NOW)
        self.assertEqual(len(chart['labels']),24)
        self.assertEqual(round(sum(v or 0 for v in chart['download'])*1073741824),500)
        self.assertIsNone(chart['download'][2])
        self.assertGreater(chart['unknown_counters'],0)

    def test_owner_and_NAS_do_not_leak(self):
        chart=series(query,dict(period='today',nas='192.0.2.2'),owner=7,now=NOW)
        self.assertTrue(all(v is None for v in chart['download']))
        own=series(query,dict(period='today'),owner=8,now=NOW)
        self.assertEqual(round(sum(v or 0 for v in own['download'])*1073741824),200)

    def test_open_custom_period(self):
        data=series(query,dict(period='custom',date_from='2026-10-20'),now=NOW)
        self.assertEqual(len(data['labels']),8)
        self.assertEqual(data['labels'][-1],'2026-10-27')

    def test_daily_card_matches_hourly_sum(self):
        from core.rate_limit import format_bytes
        with patch.object(dash,'read_series',read):
            card=dash.daily_traffic_metrics(now=NOW,timezone='Asia/Aden')
        self.assertEqual(card['traffic_total_str'],format_bytes(550))
        self.assertEqual(card['traffic_status_text'],'')

    def test_failure_is_not_zero(self):
        with patch.object(dash,'read_series',side_effect=RuntimeError('isolated failure')):
            card=dash.daily_traffic_metrics(now=NOW,timezone='Asia/Aden')
        self.assertEqual(card['traffic_total_str'],'غير متاح')

    def test_six_month_sales_match_reports(self):
        with patch.object(dash,'LEDGER',ledger):
            for owner,expected in [(None,195),(7,120),(8,25),(9999,0)]:
                actual=dash.sales_series(query,owner=owner,now=NOW)
                where='1=1' if owner is None else "e.event_kind='voucher' AND e.reseller_id=%s"
                params=() if owner is None else (owner,)
                report=sales_series(query,ledger,where,params,dict(period='custom',date_from='2026-05-01',date_to='2026-10-27'),now=NOW)
                self.assertEqual(sum(actual['sales']),expected)
                self.assertEqual(sum(v or 0 for v in report['sales']),expected)

if __name__=='__main__':unittest.main(argv=[sys.argv[0]])
