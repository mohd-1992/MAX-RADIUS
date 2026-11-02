"""Native read-only virtual accounting + HTTP, on existing isolated fixture."""
import datetime as dt
import os
import sys
import threading
import unittest
from unittest.mock import patch

assert sys.argv[1:]==['--run'] and os.environ.get('DB_NAME')=='sales266_fixture'
threading.Thread.start=lambda self:None
import services.license_guard_service as lic
active=lambda *a,**k:dict(valid=True,status='active',features={},limits={},plan_tier='Synthetic')
lic.get_active_license_status=active
lic.has_license_feature=lambda *a,**k:True
import core.license_feature_policy as feature
feature.verified_state=active
from web import create_app
from database.db import query_all,get_connection
import database.db as db
db._run_with_autoheal=lambda func,*args,**kw:func(*args,**kw)
import services.network_traffic_service as traffic

NOW=dt.datetime(2026,10,26,10,tzinfo=dt.timezone.utc)
app=create_app({'TESTING':True,'SALES_SOURCE_ENRICHMENT':False})
tables=('radacct_owned','radacct_archive_owned','wisp_imported_session_history_owned')
base=dict(radacctid=1,acctuniqueid='a',username='virtual-a',acctsessionid='a',nasipaddress='192.0.2.10',
          calledstationid='HS1',nasportid='ether1',framedprotocol='',servicetype='Login-User',
          acctstarttime='2026-10-26 01:00:00',acctstoptime=None,acctupdatetime='2026-10-26 02:00:00',
          acctinputoctets=10,acctoutputoctets=100,acctterminatecause='')
live=[base,
      dict(base,radacctid=2,acctuniqueid='b',acctsessionid='b',framedprotocol='PPP',servicetype='Framed-User',calledstationid='PPP',nasportid='bridge1',acctoutputoctets=300,acctinputoctets=30),
      dict(base,radacctid=3,acctuniqueid='c',acctsessionid='c',framedprotocol='PPP',servicetype='Framed-User',calledstationid='PPP',nasportid='bridge2',acctoutputoctets=400,acctinputoctets=40),
      dict(base,radacctid=4,acctuniqueid='d',acctsessionid='d',nasportid='ether2',acctoutputoctets=600,acctinputoctets=60),
      dict(base,radacctid=5,acctuniqueid='e',acctsessionid='e',servicetype='',calledstationid='',acctoutputoctets=50,acctinputoctets=None),
      dict(base,radacctid=6,acctuniqueid='old',acctsessionid='old',acctstarttime='2026-10-25 23:59:59',acctoutputoctets=9000,acctinputoctets=900),
      dict(base,radacctid=7,username='virtual-reused',acctoutputoctets=5,acctinputoctets=1),
      dict(base,radacctid=8,acctuniqueid='negative',acctsessionid='negative',servicetype='',calledstationid='',acctoutputoctets=-5,acctinputoctets=-1),
      dict(base,radacctid=9,acctuniqueid='undated',acctsessionid='undated',acctstarttime=None,acctoutputoctets=20,acctinputoctets=2),
      dict(base,radacctid=10,acctuniqueid='sentinel',acctsessionid='sentinel',acctterminatecause='Consolidated-Historical-Import',acctoutputoctets=100000)]
archive=[dict(base,radacctid=100,acctoutputoctets=200,acctinputoctets=20,acctstoptime='2026-10-26 03:00:00')]
imported=[dict(live[1],radacctid=101)]

def literal(value):
    if value is None:return 'NULL'
    if isinstance(value,int):return str(value)
    return "'"+str(value).replace("'","''")+"'"

def virtual_sql(owner=None,start=None,end=None,metadata_only=False):
    sql,params=original(owner,start,end,metadata_only=metadata_only)
    for table,rows in zip(tables,[live,archive,imported]):
        relation=' UNION ALL '.join('SELECT '+','.join(literal(value)+' AS '+key for key,value in dict(row,original_username=row['username']).items()) for row in rows)
        sql=sql.replace('FROM '+table+' WHERE','FROM ('+relation+') '+table+' WHERE')
    sql=sql.replace('SELECT BINARY username FROM wisp_vouchers WHERE reseller_id=%s',
        "SELECT BINARY username FROM (SELECT 'virtual-a' AS username,7 AS reseller_id) ownership WHERE reseller_id=%s")
    return sql,params

original=traffic.sessions_sql


class Native(unittest.TestCase):
    def setUp(self):
        for target,value in [('core.ownership.owner_id',None),('services.network_traffic_service.sessions_sql',virtual_sql)]:
            patcher=patch(target,return_value=value) if target.endswith('owner_id') else patch(target,side_effect=value)
            patcher.start();self.addCleanup(patcher.stop)

    def report(self,**args):return traffic.report(args,now=NOW,timezone='Asia/Aden')

    def test_deduplicated_totals_and_packet_points(self):
        data=self.report()
        self.assertEqual(data['totals']['sessions'],7)
        self.assertEqual(data['totals']['download'],1555)
        self.assertEqual(data['totals']['upload'],151)
        self.assertEqual(data['totals']['unknown_download'],1)
        self.assertEqual(data['totals']['unknown_upload'],2)
        self.assertEqual(len(data['points']),4)
        hotspot=next(p for p in data['points'] if p['service_type']=='hotspot')
        self.assertEqual(hotspot['sessions'],3)
        self.assertEqual(hotspot['download'],805)
        self.assertEqual(len([p for p in data['points'] if p['service_type']=='pppoe']),2)

    def test_period_boundaries_unknown_dates_and_later_counters(self):
        today=self.report()
        alltime=self.report(period='all')
        self.assertEqual(alltime['totals']['sessions'],9)
        self.assertEqual(alltime['totals']['unknown_date'],1)
        yesterday=self.report(period='yesterday')
        self.assertEqual(yesterday['totals']['sessions'],1)
        self.assertEqual(yesterday['totals']['download'],9000)
        self.assertEqual(today['nas_options'],yesterday['nas_options'])
        utc=traffic.report({'period':'all'},now=NOW,timezone='UTC')
        hotspot=next(p for p in utc['points'] if p['service_type']=='hotspot')
        self.assertEqual(hotspot['last_observed'],'2026-10-26 00:00:00')

    def test_filters_sorting_and_reconciliation(self):
        alltime=self.report()
        partitions=[self.report(service_type=kind) for kind in ('hotspot','pppoe','unknown')]
        self.assertEqual(sum(p['totals']['sessions'] for p in partitions),alltime['totals']['sessions'])
        for point in alltime['points']:
            data=self.report(server=point['server_key'])
            self.assertEqual(data['totals']['sessions'],point['sessions'])
        for key in traffic.SORT_KEYS:
            for direction in ('asc','desc'):
                data=self.report(traffic_sort=key,traffic_dir=direction)
                self.assertEqual(data['totals'],alltime['totals'])
        self.assertEqual(self.report(nas='192.0.2.11')['totals']['sessions'],0)

    def test_archive_source_conflicts_are_counted_once_as_unknown(self):
        with patch.dict(archive[0],calledstationid='Conflicting-HS'):
            data=self.report()
        self.assertEqual(data['totals']['sessions'],7)
        self.assertGreater(data['missing_points'],2)

    def test_restricted_scope_and_unknown_counters(self):
        with patch('core.ownership.owner_id',return_value=999999):
            data=self.report()
        self.assertEqual(data['totals']['sessions'],0)
        self.assertEqual(data['nas_options'],[])
        with patch.dict(live[6],nasipaddress='192.0.2.11'):
            with patch('core.ownership.owner_id',return_value=7):
                data=self.report()
        self.assertEqual(data['totals']['sessions'],6)
        self.assertNotIn('192.0.2.11',dict(data['nas_options']))

    def test_server_totals_without_archive_duplication(self):
        data=self.report()
        self.assertEqual(data['totals']['sessions'],7)
        self.assertEqual(data['totals']['download'],1555)
        self.assertEqual(self.report(service_type='pppoe')['totals']['sessions'],2)
        point=next(p for p in data['points'] if p['service_type']=='pppoe')
        self.assertEqual(self.report(server=point['server_key'])['totals']['total'],point['total'])
        self.assertNotIn('top_consumers',data)

    def test_all_accounts_contribute_without_a_top_ten_limit(self):
        baseline=self.report()['totals']
        extra=[dict(base,radacctid=200+i,acctuniqueid='extra'+str(i),acctsessionid='extra'+str(i),
                    username='fixture-'+str(i),acctoutputoctets=i*1000,acctinputoctets=i,
                    nasipaddress='192.0.2.11' if i%2 else '192.0.2.10') for i in range(1,21)]
        with patch.object(sys.modules[__name__],'live',live+extra):
            data=self.report()['totals'];scoped=self.report(nas='192.0.2.11')['totals']
        self.assertEqual(data['sessions'],baseline['sessions']+20)
        self.assertEqual(data['download'],baseline['download']+210000)
        self.assertEqual((scoped['sessions'],scoped['download'],scoped['upload']),(10,100000,100))

    def test_packet_labels_are_escaped_and_chart_totals_match(self):
        with patch.dict(live[2],calledstationid='<img src=x onerror=alert(1)>'):
            client=app.test_client()
            with client.session_transaction() as session:session['admin_id']=1
            response=client.get('/vouchers/sales?report_view=traffic&period=all')
        html=response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('<img src=x onerror=alert(1)>',html)
        self.assertIn('&lt;img',html)
        data=self.report()
        self.assertEqual(traffic.sum_rows(data['chart_points']),data['totals'])

    def test_http_filters_html_and_fail_closed(self):
        client=app.test_client()
        with client.session_transaction() as session:session['admin_id']=1
        response=client.get('/vouchers/sales?report_view=traffic&period=all')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('networkTrafficChart',html)
        self.assertIn('غير مكتمل',html)
        self.assertIn('report_view=traffic',html)
        self.assertNotIn('topConsumersTable',html)
        self.assertEqual(client.get('/vouchers/sales?report_view=traffic&period=invalid').status_code,400)
        with patch('services.network_traffic_service.report',side_effect=RuntimeError('synthetic unavailable')):
            response=client.get('/vouchers/sales?report_view=traffic')
        self.assertEqual(response.status_code,503)
        self.assertIn('تعذرت قراءة',response.get_data(as_text=True))


if __name__=='__main__':unittest.main(argv=[sys.argv[0]])
