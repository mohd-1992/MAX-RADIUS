"""Explicit synthetic MariaDB fixture for report repairs; no application or NAS.

Requires --run and the dedicated reports_repair_fixture database on an isolated
network. Never accepts the configured application database as a fallback.
"""
import os,sys,types,json,datetime as dt,threading,unittest
from pathlib import Path
from decimal import Decimal


def main():
    compiled=sys.argv[1:]==['--run','--compiled']
    if sys.argv[1:] not in (['--run'],['--run','--compiled']) or os.environ.get('DB_NAME')!='reports_repair_fixture':
        raise SystemExit('Requires --run and isolated reports_repair_fixture')
    import pymysql
    sys.path.insert(0,'/app' if compiled else str(Path(__file__).resolve().parents[1]))
    threading.Thread.start=lambda self:None
    def connection():return pymysql.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],password=os.environ['DB_PASSWORD'],database='reports_repair_fixture',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    def execute(sql,args=(),mode='all',conn=None):
        owned=conn is None;conn=conn or connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql.replace('?','%s'),args)
                result=cur.fetchone() if mode=='one' else list(cur.fetchall()) if mode=='all' else cur.rowcount
            if owned:conn.commit()
            return result
        finally:
            if owned:conn.close()
    db=types.ModuleType('database.db');db.get_connection=connection;db.is_mysql_conn=lambda conn:True;db.adapt_query=lambda sql,conn=None:sql.replace('?','%s');db.query_all=lambda sql,args=():execute(sql,args);db.query_one=lambda sql,args=():execute(sql,args,'one');db.execute_write=lambda sql,args=():execute(sql,args,'count')
    sys.modules['database.db']=db
    owner=types.ModuleType('core.ownership');owner.owner_id=lambda:None;sys.modules['core.ownership']=owner
    clock=types.ModuleType('core.time_service');clock.get_configured_timezone_name=lambda:'Asia/Aden';sys.modules['core.time_service']=clock
    from services import report_catalog_cache
    report_catalog_cache.catalog_query=lambda query,owner:query
    from database.reporting_schema import install_reporting_schema
    from database.traffic_samples_schema import install_traffic_samples_schema
    from database.usage_summary_schema import install_usage_summary_schema
    from database.sales_source_schema import TABLE_SQL as SOURCE_DDL,install_sales_source_schema,ACTIVATION_SOURCE_SQL
    conn=connection()
    with conn.cursor() as cur:
        cur.execute('SHOW TABLES');existing={next(iter(r.values())) for r in cur.fetchall()}
        if existing and 'max_reports_fixture_guard' not in existing:raise RuntimeError('Refusing non-fixture database')
        cur.execute("CREATE TABLE IF NOT EXISTS max_reports_fixture_guard (marker VARCHAR(64) PRIMARY KEY)")
        cur.execute("INSERT IGNORE INTO max_reports_fixture_guard VALUES ('reports-repair-20261110')")
        cur.execute('''CREATE TABLE IF NOT EXISTS radacct (radacctid BIGINT PRIMARY KEY,acctuniqueid VARCHAR(32),username VARCHAR(100),acctsessionid VARCHAR(64),nasipaddress VARCHAR(45),calledstationid VARCHAR(128),nasportid VARCHAR(128),framedprotocol VARCHAR(32),servicetype VARCHAR(32),acctstarttime DATETIME,acctstoptime DATETIME,acctupdatetime DATETIME,acctinputoctets BIGINT,acctoutputoctets BIGINT,acctsessiontime BIGINT DEFAULT 0,acctterminatecause VARCHAR(64),reseller_id BIGINT,account_voucher_id BIGINT,account_subscriber_id BIGINT,account_username VARCHAR(100),account_read_username VARCHAR(100),framedipaddress VARCHAR(45),callingstationid VARCHAR(64),source_fallback VARCHAR(96) DEFAULT '')''')
        cur.execute('CREATE OR REPLACE VIEW radacct_owned AS SELECT a.*,a.username AS original_username FROM radacct a')
        for table in ('radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
            cur.execute('CREATE TABLE IF NOT EXISTS '+table+' LIKE radacct')
            cur.execute('CREATE OR REPLACE VIEW '+table+'_owned AS SELECT a.*,a.username AS original_username FROM '+table+' a')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_vouchers (id BIGINT PRIMARY KEY,username VARCHAR(100),reseller_id BIGINT,package_id BIGINT,status VARCHAR(32),last_renewed_at DATETIME,first_used_at DATETIME,created_at DATETIME)')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_subscribers (id BIGINT PRIMARY KEY,username VARCHAR(100))')
        cur.execute('ALTER TABLE wisp_subscribers ADD COLUMN IF NOT EXISTS package_id BIGINT, ADD COLUMN IF NOT EXISTS service_type VARCHAR(16)')
        cur.execute('ALTER TABLE wisp_vouchers ADD COLUMN IF NOT EXISTS serial_number VARCHAR(100)')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_subscriber_names (subscriber_id BIGINT,username VARCHAR(100))')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_voucher_names (voucher_id BIGINT,username VARCHAR(100))')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_session_baselines (radacctid BIGINT)')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_packages (id BIGINT PRIMARY KEY,name VARCHAR(100),price DECIMAL(12,2),cost DECIMAL(12,2),service_type VARCHAR(16))')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_invoices (id BIGINT PRIMARY KEY,invoice_number VARCHAR(64),subscriber_id BIGINT,subscriber_name VARCHAR(100),package_name VARCHAR(100),amount DECIMAL(12,2),status VARCHAR(16),paid_at DATETIME,created_at DATETIME,notes TEXT)')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_voucher_sales (id BIGINT PRIMARY KEY,voucher_id BIGINT,first_activation_voucher_id BIGINT,batch_id BIGINT,batch_name VARCHAR(100),username VARCHAR(100),serial_number VARCHAR(100),package_name VARCHAR(100),price DECIMAL(12,2),cost DECIMAL(12,2),reseller_id BIGINT,activated_at DATETIME)')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_managers (id BIGINT PRIMARY KEY,username VARCHAR(100),full_name VARCHAR(100))')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_nas_devices (id BIGINT PRIMARY KEY,ip_address VARCHAR(45),name VARCHAR(100))')
        cur.execute('CREATE TABLE IF NOT EXISTS nas (id BIGINT PRIMARY KEY,nasname VARCHAR(45),shortname VARCHAR(100))')
        cur.execute('CREATE TABLE IF NOT EXISTS wisp_system_settings (`key` VARCHAR(100) PRIMARY KEY,`value` TEXT,description TEXT)')
        cur.execute(SOURCE_DDL)
        for name,definition in [('api_username','VARCHAR(100)'),('api_password','VARCHAR(100)'),('api_port','INT')]:
            cur.execute('ALTER TABLE wisp_nas_devices ADD COLUMN IF NOT EXISTS '+name+' '+definition)
        for table in ['radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history','wisp_vouchers','wisp_subscribers']:
            cur.execute('ALTER TABLE '+table+' CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        for table in ['wisp_nas_devices','nas']:
            cur.execute('ALTER TABLE '+table+' CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    install_sales_source_schema(conn);install_reporting_schema(conn);install_traffic_samples_schema(conn);install_usage_summary_schema(conn);conn.commit();conn.close()
    from services import network_traffic_service as network,sales_reporting_service as sales,traffic_sample_service as samples
    if compiled:
        import database.reporting_schema as reporting_schema
        for module in (network,sales,samples,reporting_schema):
            if not module.__file__.startswith('/app/') or not module.__file__.endswith('.so'):raise RuntimeError('Expected compiled report module')
    from services.invoice_cost_service import record_invoice_cost
    NOW=dt.datetime(2026,11,10,12,tzinfo=dt.timezone.utc)
    class Repair(unittest.TestCase):
        def setUp(self):
            conn=connection()
            with conn.cursor() as cur:
                for table in ['radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history','wisp_account_usage_seals','wisp_account_usage_summary','wisp_sale_sources','wisp_sale_source_audit','wisp_invoices','wisp_voucher_sales','wisp_vouchers','wisp_subscribers','wisp_packages','wisp_nas_devices','nas','wisp_traffic_intervals','wisp_traffic_checkpoints','wisp_traffic_rollups','wisp_session_baselines','wisp_voucher_names','wisp_subscriber_names','wisp_session_service_evidence']:
                    cur.execute('DELETE FROM '+table)
                cur.execute("INSERT INTO wisp_vouchers(id,username,reseller_id,package_id,status,last_renewed_at,first_used_at,created_at) VALUES(1,'fixture',NULL,1,'expired','2026-10-01',NULL,'2026-10-01')")
                cur.execute("INSERT INTO wisp_packages VALUES(1,'fixture-package',6000,5000,'both')")
                cur.execute("UPDATE wisp_traffic_rollup_state SET started_at='2026-01-01' WHERE id=1")
            conn.commit();conn.close()
        def row(self,id=1,start=dt.datetime(2026,11,10,10),down=100,up=20,**changes):
            row=dict(radacctid=id,acctuniqueid=format(id,'032x'),username='fixture',acctsessionid='session-'+str(id),nasipaddress='192.0.2.10',calledstationid='HS',nasportid='ether1',framedprotocol='',servicetype='Login-User',acctstarttime=start,acctstoptime=start+dt.timedelta(minutes=1),acctupdatetime=start+dt.timedelta(minutes=1),acctinputoctets=up,acctoutputoctets=down,acctsessiontime=60,acctterminatecause='User-Request',account_voucher_id=1,account_subscriber_id=None,account_username='fixture',account_read_username='fixture',reseller_id=None)
            row.update(changes);return row
        def insert(self,table,row):
            execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES('+','.join('%s' for _ in row)+')',tuple(row.values()),'count')
        def query(self,sql,args=()):return execute(sql,args)
        def test_cost_snapshot_zero_unknown_and_original_amount(self):
            self.insert('wisp_invoices',dict(id=1,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,10),invoice_number='F1'))
            conn=connection();record_invoice_cost(conn,1,dict(cost=5000,service_type='both'));conn.commit();record_invoice_cost(conn,1,dict(cost=2000,service_type='hotspot'));conn.commit();conn.close()
            row=execute('SELECT amount,report_cost,package_service_type FROM wisp_invoices WHERE id=1',mode='one');self.assertEqual((row['amount'],row['report_cost'],row['package_service_type']),(6000,5000,'both'))
            data=sales.report({'record_kind':'invoice','period':'all'},export=True,now=NOW,timezone='Asia/Aden');self.assertEqual(data['gross_sales'],5000)
        def test_old_invoice_uses_original_amount_not_current_cost(self):
            self.insert('wisp_invoices',dict(id=1,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,10),invoice_number='F1'))
            data=sales.report({'record_kind':'invoice','period':'all'},export=True,now=NOW,timezone='Asia/Aden');self.assertEqual(data['unknown_amount_count'],0);self.assertEqual(data['recent_sales'][0]['price'],6000)
        def test_zero_cost_is_recorded_not_missing(self):
            self.insert('wisp_invoices',dict(id=1,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,10),invoice_number='F1'))
            conn=connection();record_invoice_cost(conn,1,dict(cost=0,service_type='hotspot'));conn.commit();conn.close()
            data=sales.report({'record_kind':'invoice','period':'all'},export=True,now=NOW,timezone='Asia/Aden');self.assertEqual(data['gross_sales'],0);self.assertEqual(data['unknown_amount_count'],0);self.assertEqual(data['recent_sales'][0]['price'],0)
        def test_mixed_old_and_new_invoice_values_are_read_only_and_consistent(self):
            for id,amount,cost,source in [(1,6000,None,''),(2,9000,5000,'package_snapshot'),(3,1000,0,'package_snapshot'),(4,7000,None,'unavailable')]:
                self.insert('wisp_invoices',dict(id=id,amount=amount,report_cost=cost,cost_source=source,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,10),invoice_number='F'+str(id)))
            self.insert('wisp_invoices',dict(id=5,amount=800,status='unpaid',invoice_number='F5'))
            self.insert('wisp_invoices',dict(id=6,amount=900,status='paid',package_name='إيداع رصيد محفظة',paid_at=dt.datetime(2026,11,10),invoice_number='F6'))
            before=execute('SELECT * FROM wisp_invoices ORDER BY id')
            execute('UPDATE wisp_packages SET cost=123 WHERE id=1',mode='count')
            for export in (False,True):
                data=sales.report(dict(record_kind='invoice',period='all'),export=export,now=NOW,timezone='Asia/Aden')
                self.assertEqual((data['total_items_count'],data['gross_sales'],data['net_isp_revenue'],data['unknown_amount_count']),(4,11000,11000,1))
                self.assertEqual({r['id']:r['price'] for r in data['recent_sales']},{1:6000,2:5000,3:0,4:None})
                if not export:
                    self.assertEqual(sum(v or 0 for v in data['sales_chart']['sales']),11000)
                    self.assertEqual(sum(r['total_gross_revenue'] for r in data['server_breakdown']),11000)
            self.assertEqual(execute('SELECT * FROM wisp_invoices ORDER BY id'),before)
        def test_report_presentation_fixtures(self):
            from tests.test_reports_repair_ui import render,check_navigation
            args=dict(record_kind='invoice',period='custom',date_from='2026-08-15',date_to='2026-11-05',nas='192.0.2.10',service_type='unknown',page='3')
            for id in (1,2):
                self.insert('wisp_invoices',dict(id=id,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,1),invoice_number='F'+str(id)))
                self.insert('wisp_sale_sources',dict(event_kind='invoice',event_id=id,nas_address='192.0.2.10',package_service_type='both'))
            execute("UPDATE wisp_invoices SET cost_source='unavailable' WHERE id=1",mode='count')
            unknown=sales.report(args,now=NOW,timezone='Asia/Aden');html=render('sales_reports.html',unknown,args)
            self.assertIn('غير متاح',html);self.assertIn('Hotspot / PPPoE (الباقة)',html);check_navigation(html,args)
            conn=connection();record_invoice_cost(conn,2,dict(cost=5000,service_type='both'));conn.commit();conn.close()
            partial=sales.report(args,now=NOW,timezone='Asia/Aden');self.assertEqual((partial['gross_sales'],partial['unknown_amount_count']),(5000,1))
            from services.traffic_interval_service import save_interval
            conn=connection()
            with conn.cursor() as cur:
                save_interval(cur,'f'*64,dict(observed_at=dt.datetime(2026,11,9,23,58)),dt.datetime(2026,11,10,0,6),dict(server_key='a'*64,nas_address='192.0.2.10',service_type='',server_name='Fixture HS',server_interface='',point_known=False),0,100,20,'delayed_sample')
            conn.commit();conn.close()
            traffic=network.report({},now=NOW,timezone='Asia/Aden');traffic_html=render('network_traffic.html',traffic,{})
            self.assertNotIn('جزئي',traffic_html);self.assertIn('غير موثق',traffic_html);self.assertNotIn('جلسة دون Stop',traffic_html)
            self.assertIn('تعذرت قراءة استهلاك الشبكة',render('network_traffic.html',dict(traffic_unavailable=True),{}))
            self.insert('radacct',self.row());session_args=dict(basis='sessions');sessions=network.report(session_args,now=NOW,timezone='Asia/Aden')
            self.assertIn('جلسة دون Stop',render('network_traffic.html',sessions,session_args))
            self.point_rollup();self.point_rollup('hotspot',50);resolved=network.report({},now=NOW,timezone='Asia/Aden')
            self.assertEqual(sum(r['server_name']=='HS' for r in resolved['points']),1)
            self.insert('wisp_voucher_sales',dict(id=10,voucher_id=1,username='fixture',price=100,cost=80,activated_at=dt.datetime(2026,10,12)))
            self.insert('wisp_invoices',dict(id=10,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,10,5),invoice_number='F10'))
            from services.dashboard_reporting_service import sales_series
            resources=dict(cpu_percent=.5,cpu_available=False,ram_percent=1.,ram_used_gb=0.,ram_total_gb=8.,disk_percent=1.,disk_used_gb=1.,disk_total_gb=100.,containers={})
            dashboard=dict(settings={},metrics=dict(resources=resources,voucher_summary={}),traffic_chart=dict(labels=[],download=[],upload=[]),sales_chart=sales_series(self.query,now=NOW),latest_operations=[],system_uptime='اختبار',internet_status={})
            self.assertNotIn('جزئي',render('dashboard.html',dashboard,{}))
            if os.environ.get('REPORTS_FIXTURE_OUTPUT'):
                def serial(value):
                    if isinstance(value,Decimal):return float(value)
                    if isinstance(value,(dt.date,dt.datetime)):return value.isoformat()
                    raise TypeError(type(value).__name__)
                Path(os.environ['REPORTS_FIXTURE_OUTPUT']).write_text(json.dumps([dict(template='sales_reports.html',data=unknown,args=args,name='invoice-unknown'),dict(template='sales_reports.html',data=partial,args=args,name='invoice-partial'),dict(template='network_traffic.html',data=traffic,args={},name='measured-partial'),dict(template='network_traffic.html',data=sessions,args=session_args,name='sessions'),dict(template='network_traffic.html',data=resolved,args={},name='resolved-hotspot'),dict(template='dashboard.html',data=dashboard,args={},name='october-dashboard')],default=serial,ensure_ascii=False),encoding='utf8')
        def test_additive_migration_does_not_reprice_history(self):
            self.insert('wisp_invoices',dict(id=1,amount=6000,status='paid',package_name='fixture-package',invoice_number='F1'))
            conn=connection();install_reporting_schema(conn);install_reporting_schema(conn);conn.commit();conn.close()
            value=execute('SELECT amount,report_cost FROM wisp_invoices WHERE id=1',mode='one');self.assertEqual(value['amount'],6000);self.assertIsNone(value['report_cost'])
        def test_invoice_snapshot_rolls_back_with_operation(self):
            conn=connection()
            execute("INSERT INTO wisp_invoices(id,amount,status) VALUES(1,6000,'paid')",mode='count',conn=conn)
            record_invoice_cost(conn,1,dict(cost=5000,service_type='both'));conn.rollback();conn.close()
            self.assertEqual(execute('SELECT COUNT(*) n FROM wisp_invoices',mode='one')['n'],0)
        def test_actual_csv_excel_export_legacy_amount_and_new_cost_are_consistent(self):
            import ast,csv,io,tempfile
            from unittest.mock import patch
            from flask import Flask,request,jsonify,send_file
            from openpyxl import load_workbook
            for id in (1,2):self.insert('wisp_invoices',dict(id=id,amount=6000,status='paid',invoice_number='F'+str(id),package_name='fixture-package',paid_at=dt.datetime(2026,11,10)))
            conn=connection();record_invoice_cost(conn,2,dict(cost=5000,service_type='both'));conn.commit();conn.close()
            source=Path('/workspace/web/routes/system.py');node=next(n for n in ast.parse(source.read_text(encoding='utf8')).body if isinstance(n,ast.FunctionDef) and n.name=='export_sales');node.decorator_list=[]
            ns=dict(request=request,jsonify=jsonify,send_file=send_file,datetime=dt)
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
            heavy=types.ModuleType('services.heavy_operation_service');heavy.temporary_output=lambda:tempfile.SpooledTemporaryFile(max_size=1024*1024,mode='w+b')
            app=Flask('fixture-exports');app.add_url_rule('/export',view_func=ns['export_sales'])
            with patch.dict(sys.modules,{'services.heavy_operation_service':heavy}):
                csv_response=app.test_client().get('/export?record_kind=invoice&period=all&format=csv');self.assertEqual(csv_response.status_code,200)
                rows=list(csv.reader(io.StringIO(csv_response.data.decode('utf-8-sig'))));self.assertEqual(rows[0][6],'تكلفة الاشتراك المحفوظة');self.assertEqual({r[6] for r in rows[1:-1]},{'5000.00','6000.00'});self.assertEqual(rows[-1][0],'الإجمالي')
                xlsx=app.test_client().get('/export?record_kind=invoice&period=all&format=excel');self.assertEqual(xlsx.status_code,200)
                book=load_workbook(io.BytesIO(xlsx.data),read_only=True,data_only=True);values=list(book.active.values);self.assertEqual({r[6] for r in values[1:-1]},{5000,6000});self.assertEqual(values[-1][0],'الإجمالي');book.close()
            self.assertEqual(execute('SELECT SUM(amount) n FROM wisp_invoices',mode='one')['n'],12000)
        def test_activation_trigger_uses_packet_not_package_capability(self):
            conn=connection();install_sales_source_schema(conn);install_sales_source_schema(conn)
            with conn.cursor() as cur:
                cur.execute('DROP TRIGGER IF EXISTS max_fixture_activation_source')
                cur.execute('CREATE TRIGGER max_fixture_activation_source AFTER INSERT ON radacct FOR EACH ROW BEGIN DECLARE v_id BIGINT; DECLARE v_package_id BIGINT; SET v_id=NEW.account_voucher_id,v_package_id=1; '+ACTIVATION_SOURCE_SQL+' END')
            try:
                for id,protocol,service in [(1,'',''),(2,'PPP',''),(3,'PPP','Login-User')]:
                    self.insert('wisp_voucher_sales',dict(id=id,first_activation_voucher_id=id,price=6000,cost=5000))
                    self.insert('radacct',self.row(id,framedprotocol=protocol,servicetype=service,account_voucher_id=id))
                rows=execute('SELECT event_id,service_type,package_service_type FROM wisp_sale_sources ORDER BY event_id')
                self.assertEqual([(r['service_type'],r['package_service_type']) for r in rows],[('','both'),('pppoe','both'),('','both')])
                self.assertEqual(execute('SELECT SUM(price) n FROM wisp_voucher_sales',mode='one')['n'],18000)
            finally:
                with conn.cursor() as cur:cur.execute('DROP TRIGGER max_fixture_activation_source')
                conn.close()
        def test_verified_service_enriches_sale_metadata_only(self):
            from services.sales_source_service import record_sale_source,enrich_sale_sources
            from services.session_service_evidence import record_evidence
            row=self.row(servicetype='',account_subscriber_id=1,account_voucher_id=None)
            self.insert('radacct',row);self.insert('wisp_subscribers',dict(id=1,username='fixture',package_id=1,service_type='both'))
            self.insert('wisp_invoices',dict(id=1,subscriber_id=1,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,11,10,10,0,30)))
            conn=connection();record_invoice_cost(conn,1,dict(cost=5000,service_type='both'));record_sale_source(conn,'invoice',1,'fixture');conn.commit()
            first=execute('SELECT service_type,package_service_type FROM wisp_sale_sources',mode='one');self.assertEqual(first,dict(service_type='',package_service_type='both'))
            record_evidence(conn,row,'hotspot',dt.datetime(2026,11,10,10,1));conn.commit();result=enrich_sale_sources(conn);conn.close()
            self.assertEqual(result['confirmed'],1);self.assertEqual(execute('SELECT service_type FROM wisp_sale_sources',mode='one')['service_type'],'hotspot')
            self.assertEqual(execute('SELECT amount,report_cost FROM wisp_invoices',mode='one'),dict(amount=6000,report_cost=5000))
            self.assertEqual(execute('SELECT servicetype,acctoutputoctets,acctinputoctets FROM radacct',mode='one'),dict(servicetype='',acctoutputoctets=100,acctinputoctets=20))
            self.assertEqual(execute('SELECT COUNT(*) n FROM wisp_sale_source_audit',mode='one')['n'],1)
        def test_purge_preserves_verified_service_metadata(self):
            from services.session_service_evidence import record_evidence
            from services.expired_voucher_purge import purge_details
            row=self.row(servicetype='');self.insert('radacct',row);conn=connection();record_evidence(conn,row,'hotspot',dt.datetime(2026,11,10));conn.commit()
            account=execute('SELECT * FROM wisp_vouchers WHERE id=1',mode='one');purge_details(conn,account,history_only=True,cutoff=dt.datetime(2026,11,12));conn.commit();conn.close()
            value=execute('SELECT servicetype,input_bytes,output_bytes FROM wisp_account_usage_summary',mode='one');self.assertEqual(value,dict(servicetype='Login-User',input_bytes=20,output_bytes=100))
        def test_default_consumption_uses_dashboard_basis(self):
            self.insert('wisp_traffic_rollups',dict(bucket_at=dt.datetime(2026,11,10,10),owner_scope=0,point_key='a'*64,nas_address='192.0.2.10',download_bytes=250,upload_bytes=50,download_known=1,upload_known=1,observations=1))
            data=network.report({},now=NOW,timezone='Asia/Aden');daily=samples.daily_rows(self.query,days=1,now=NOW)[0]
            self.assertEqual(data['selected']['basis'],'measured');self.assertEqual(data['totals']['total'],daily['download']+daily['upload']);self.assertEqual(data['totals']['total'],300)
        def test_october_sales_survive_missing_historical_invoice_cost(self):
            from services.dashboard_reporting_service import sales_series
            self.insert('wisp_voucher_sales',dict(id=1,voucher_id=1,username='fixture',price=100,cost=80,activated_at=dt.datetime(2026,10,12)))
            self.insert('wisp_invoices',dict(id=1,amount=6000,status='paid',package_name='fixture-package',paid_at=dt.datetime(2026,10,5),invoice_number='F1'))
            chart=sales_series(self.query,now=NOW);index=chart['labels'].index('أكتوبر')
            self.assertEqual((chart['sales'][index],chart['voucher_sales'][index],chart['package_sales'][index]),(6100,100,6000))
            report=sales.report(dict(record_kind='invoice',period='all'),export=True,now=NOW,timezone='Asia/Aden');self.assertEqual(report['recent_sales'][0]['price'],6000);self.assertEqual(report['unknown_amount_count'],0)
            self.assertEqual(execute('SELECT amount FROM wisp_invoices',mode='one')['amount'],6000)
        def point_rollup(self,kind='',value=100,owner=0,nas='192.0.2.10',name='HS',interface=''):
            point=network.point_info(dict(nas_address=nas,server_name=name,service_type=kind,server_interface=interface),{})
            self.insert('wisp_traffic_rollups',dict(bucket_at=dt.datetime(2026,11,10,10),owner_scope=owner,point_key=point['server_key'],nas_address=point['nas_address'],service_type=kind,server_name=name,server_interface=interface,point_known=int(point['point_known']),download_bytes=value,upload_bytes=0,download_known=1,upload_known=1,observations=1))
            return point
        def test_refinement_merges_old_and_new_rows_and_filters(self):
            old=self.point_rollup();known=self.point_rollup('hotspot',50)
            for args in ({},{'service_type':'hotspot'},{'server':old['server_key']},{'server':known['server_key']}):
                data=network.report(args,now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['points']),1);self.assertEqual(data['totals']['download'],150);self.assertEqual(data['points'][0]['service_type'],'hotspot');self.assertEqual(len(data['server_options']),1)
            self.assertEqual(len(network.report({'service_type':'unknown'},now=NOW,timezone='Asia/Aden')['points']),0)
            chart=samples.series(self.query,dict(period='today',service_type='hotspot'),now=NOW);self.assertEqual(round(sum(v or 0 for v in chart['download'])*2**30),150)
            self.assertEqual(execute("SELECT SUM(download_bytes) n FROM wisp_traffic_rollups WHERE service_type='hotspot'",mode='one')['n'],50)
        def test_conflicting_point_namespaces_are_not_merged(self):
            self.point_rollup();self.point_rollup('hotspot',50);self.point_rollup('pppoe',25,interface='bridge1')
            data=network.report({},now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['points']),3);self.assertEqual(data['totals']['download'],175)
        def test_incomplete_ppp_namespace_blocks_hotspot_reclassification(self):
            self.point_rollup();self.point_rollup('hotspot',50);self.point_rollup('pppoe',25)
            data=network.report({},now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['points']),3);self.assertEqual(data['totals']['download'],175)
        def test_another_owner_cannot_supply_classification(self):
            from unittest.mock import patch
            self.point_rollup(owner=7);self.point_rollup('hotspot',50,owner=8)
            with patch('core.ownership.owner_id',return_value=7):data=network.report({},now=NOW,timezone='Asia/Aden')
            self.assertEqual(len(data['points']),1);self.assertEqual(data['points'][0]['service_type'],'');self.assertEqual(data['totals']['download'],100)
        def test_known_interval_supplies_point_identity_without_adding_bytes_twice(self):
            old=self.point_rollup();known=network.point_info(dict(nas_address=old['nas_address'],server_name='HS',service_type='hotspot'),{})
            self.insert('wisp_traffic_intervals',dict(interval_key='f'*64,start_at=dt.datetime(2026,11,10,9),end_at=dt.datetime(2026,11,10,10),owner_scope=0,point_key=known['server_key'],nas_address=known['nas_address'],service_type='hotspot',server_name='HS',point_known=1,download=50,upload=0))
            data=network.report({},now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['points']),1);self.assertEqual(data['totals']['download'],150)
        def test_metadata_only_refinement_creates_no_observation_or_move(self):
            first=self.row(servicetype='',acctstoptime=None,acctupdatetime=dt.datetime(2026,11,10,10,1))
            conn=connection();samples.capture_rows(conn,[first],dt.datetime(2026,1,1));known=dict(first,servicetype='Login-User');samples.capture_rows(conn,[known],dt.datetime(2026,1,1));samples.capture_rows(conn,[known],dt.datetime(2026,1,1));conn.commit();conn.close()
            self.assertEqual(execute('SELECT SUM(observations) n FROM wisp_traffic_rollups',mode='one')['n'],1)
            self.assertEqual(execute('SELECT COUNT(*) n FROM wisp_traffic_intervals',mode='one')['n'],0)
            conn=connection();next_row=dict(known,acctoutputoctets=160,acctinputoctets=30,acctupdatetime=dt.datetime(2026,11,10,10,2));samples.capture_rows(conn,[next_row],dt.datetime(2026,1,1));conn.commit();conn.close()
            data=network.report({},now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['points']),1);self.assertEqual(data['totals']['total'],190);self.assertEqual(data['totals']['sessions'],2)
        def test_real_server_move_remains_unallocated(self):
            first=self.row(servicetype='',calledstationid='HS-A',acctstoptime=None,acctupdatetime=dt.datetime(2026,11,10,10,1))
            conn=connection();samples.capture_rows(conn,[first],dt.datetime(2026,1,1));after=dict(first,servicetype='Login-User',calledstationid='HS-B',acctoutputoctets=160,acctupdatetime=dt.datetime(2026,11,10,10,2));samples.capture_rows(conn,[after],dt.datetime(2026,1,1));conn.commit();conn.close()
            value=execute('SELECT reason,server_name,download FROM wisp_traffic_intervals',mode='one');self.assertEqual(value,dict(reason='point_change',server_name='',download=60))
        def test_named_session_mode_remains_available(self):
            self.insert('radacct',self.row(down=150,up=30))
            data=network.report({'basis':'sessions'},now=NOW,timezone='Asia/Aden');self.assertEqual(data['totals']['total'],180)
        def test_measured_owner_scope_does_not_leak_other_devices(self):
            from unittest.mock import patch
            for owner,address,value in [(7,'192.0.2.7',100),(8,'192.0.2.8',900)]:
                self.insert('wisp_traffic_rollups',dict(bucket_at=dt.datetime(2026,11,10,10),owner_scope=owner,point_key=str(owner)*64,nas_address=address,download_bytes=value,upload_bytes=0,download_known=1,upload_known=1,observations=1))
            with patch('core.ownership.owner_id',return_value=7):data=network.report({},now=NOW,timezone='Asia/Aden')
            self.assertEqual(data['totals']['total'],100);self.assertEqual([r[0] for r in data['nas_options']],['192.0.2.7'])
        def test_service_evidence_uses_exact_nas_identity(self):
            from services.session_service_evidence import matched_service,record_evidence
            row=self.row(servicetype='',acctstoptime=None,acctstarttime=dt.datetime(2026,11,10,10),acctupdatetime=dt.datetime(2026,11,10,10,10),framedipaddress='192.0.2.30',callingstationid='AA:BB:CC:DD:EE:01')
            live=dict(user='fixture',address='192.0.2.30',**{'mac-address':'AA:BB:CC:DD:EE:01'},uptime='10m',radius='true');now=dt.datetime(2026,11,10,10,10)
            self.assertEqual(matched_service(row,[live],[],now),'hotspot');self.assertEqual(matched_service(row,[dict(live,address='192.0.2.31')],[],now),'')
            self.insert('radacct',row);conn=connection();self.assertTrue(record_evidence(conn,row,'hotspot',now));conn.commit();conn.close()
            sql,args=network.sessions_sql();values=execute('SELECT service_type,download FROM '+sql,args);self.assertEqual(values[0]['service_type'],'hotspot');self.assertEqual(values[0]['download'],100)
            self.assertEqual(execute('SELECT servicetype FROM radacct',mode='one')['servicetype'],'')
        def test_conflicting_service_evidence_is_not_overwritten(self):
            from services.session_service_evidence import record_evidence
            row=self.row(servicetype='');conn=connection();self.assertTrue(record_evidence(conn,row,'hotspot',dt.datetime(2026,11,10)));self.assertFalse(record_evidence(conn,row,'pppoe',dt.datetime(2026,11,10)));conn.commit();conn.close()
            evidence=execute('SELECT service_type,source FROM wisp_session_service_evidence',mode='one');self.assertEqual(evidence,dict(service_type='',source='conflicting_evidence'))
        def test_api_enrichment_reads_only_and_preserves_counters(self):
            from services.session_service_evidence import refresh
            now=execute('SELECT CURRENT_TIMESTAMP now',mode='one')['now'];row=self.row(servicetype='',acctstoptime=None,acctstarttime=now-dt.timedelta(minutes=10),acctupdatetime=now,framedipaddress='192.0.2.30',callingstationid='AA:BB:CC:DD:EE:01');self.insert('radacct',row)
            self.insert('wisp_nas_devices',dict(id=1,ip_address='192.0.2.10',name='fixture',api_username='fixture',api_password='fixture',api_port=8728));calls=[]
            class API:
                def __init__(self,*a,**k):pass
                def connect(self):pass
                def login(self,*a):return True
                def close(self):pass
                def execute_command(self,command,words):
                    calls.append(command)
                    return [dict(user='fixture',address='192.0.2.30',**{'mac-address':'AA:BB:CC:DD:EE:01'},uptime='10m',radius='true')] if command=='/ip/hotspot/active/print' else []
            self.assertEqual(refresh(api_factory=API),1);self.assertEqual(calls,['/ip/hotspot/active/print','/ppp/active/print']);value=execute('SELECT acctinputoctets,acctoutputoctets,acctstoptime FROM radacct',mode='one');self.assertEqual(value,dict(acctinputoctets=20,acctoutputoctets=100,acctstoptime=None))
        def test_unreachable_nas_does_not_starve_other_devices(self):
            from services import session_service_evidence as evidence
            evidence._nas_cursor=0;now=execute('SELECT CURRENT_TIMESTAMP now',mode='one')['now'];calls=[]
            for id in (1,2,3):
                address='192.0.2.'+str(id)
                self.insert('radacct',self.row(id,servicetype='',acctstoptime=None,acctupdatetime=now,nasipaddress=address))
                self.insert('wisp_nas_devices',dict(id=id,ip_address=address,api_username='fixture'))
            class Offline:
                def __init__(self,address,**kwargs):calls.append(address)
                def connect(self):raise OSError('synthetic offline')
                def close(self):pass
            evidence.refresh(max_nas=1,api_factory=Offline);evidence.refresh(max_nas=1,api_factory=Offline);evidence.refresh(max_nas=1,api_factory=Offline)
            self.assertEqual(calls,['192.0.2.1','192.0.2.2','192.0.2.3'])
        def test_new_summaries_preserve_start_instants_across_timezone(self):
            from services.expired_voucher_purge import purge_details
            self.insert('radacct',self.row(1,dt.datetime(2026,11,1,1),100,10));self.insert('radacct',self.row(2,dt.datetime(2026,11,1,10),200,20))
            account=execute('SELECT * FROM wisp_vouchers WHERE id=1',mode='one');conn=connection();receipt=purge_details(conn,account,history_only=True,cutoff=dt.datetime(2026,11,3));self.assertFalse(receipt['deferred']);conn.commit();conn.close();self.assertEqual(receipt['summary_groups'],2)
            for date,expected in [('2026-10-31',110),('2026-11-01',220)]:
                start,end=sales.date_bounds('custom',date,date,timezone='UTC');sql,args=network.sessions_sql(start=start,end=end);value=execute('SELECT SUM(download+upload) total FROM '+sql,args)[0]['total'];self.assertEqual(value,expected)
        def test_long_custom_chart_is_clipped(self):
            for day,value in [(1,100),(5,200),(6,900)]:
                self.insert('wisp_traffic_rollups',dict(bucket_at=dt.datetime(2026,11,day),owner_scope=0,point_key='a'*64,download_bytes=value,upload_bytes=0,download_known=1,upload_known=1,observations=1))
            data=samples.series(self.query,dict(period='custom',date_from='2026-08-15',date_to='2026-11-05'),now=NOW)
            self.assertEqual(round(sum(v or 0 for v in data['download'])*2**30),300)
        def test_unknown_server_does_not_mix_routers(self):
            for id,nas in [(1,'192.0.2.10'),(2,'192.0.2.11')]:
                self.insert('wisp_voucher_sales',dict(id=id,voucher_id=1,username='fixture',package_name='fixture-package',price=100*id,cost=80,reseller_id=None,activated_at=dt.datetime(2026,11,10)))
                self.insert('wisp_sale_sources',dict(event_kind='voucher',event_id=id,server_key='unknown',nas_address=nas))
            data=sales.report({'period':'today'},now=NOW,timezone='Asia/Aden');self.assertEqual(len(data['server_breakdown']),2);self.assertEqual(data['gross_sales'],300)
        def test_late_counter_delta_survives_summary_without_double_session(self):
            self.insert('radacct',self.row(down=150,up=30));self.insert('wisp_account_usage_seals',dict(acctuniqueid=format(1,'032x'),voucher_id=1,input_bytes=20,output_bytes=100,uptime_seconds=60))
            self.insert('wisp_account_usage_summary',dict(summary_key='c'*64,voucher_id=1,username='fixture',first_start=dt.datetime(2026,11,10,10),last_start=dt.datetime(2026,11,10,10),input_bytes=20,output_bytes=100,uptime_seconds=60,session_count=1))
            sql,args=network.sessions_sql();rows=execute('SELECT SUM(download) down,SUM(upload) up,SUM(session_weight) n FROM '+sql,args);self.assertEqual((rows[0]['down'],rows[0]['up'],rows[0]['n']),(150,30,1))
        def test_legacy_summary_partial_day_is_not_guessed(self):
            self.insert('wisp_account_usage_summary',dict(summary_key='c'*64,voucher_id=1,username='fixture',first_start=dt.datetime(2026,11,1,1),last_start=dt.datetime(2026,11,1,10),input_bytes=30,output_bytes=300,uptime_seconds=60,session_count=2))
            sql,args=network.sessions_sql(start=dt.datetime(2026,11,1,3),end=dt.datetime(2026,11,2,3));r=execute('SELECT download,session_weight,unknown_sessions FROM '+sql,args)[0];self.assertIsNone(r['download']);self.assertEqual(r['unknown_sessions'],2)
        def test_delayed_same_quarter_keeps_all_bytes(self):
            first=self.row(down=100,up=20,acctstoptime=None,acctupdatetime=dt.datetime(2026,11,10,10,1),acctstarttime=dt.datetime(2026,11,10,10))
            conn=connection();samples.capture_rows(conn,[first],dt.datetime(2026,1,1));second=dict(first,acctoutputoctets=250,acctinputoctets=50,acctupdatetime=dt.datetime(2026,11,10,10,7));samples.capture_rows(conn,[second],dt.datetime(2026,1,1));conn.commit();conn.close()
            r=execute('SELECT SUM(download_bytes) d,SUM(upload_bytes) u FROM wisp_traffic_rollups',mode='one');self.assertEqual((r['d'],r['u']),(250,50))
        def test_delayed_cross_midnight_is_preserved_and_not_split(self):
            first=self.row(down=0,up=0,acctstoptime=None,acctstarttime=dt.datetime(2026,11,9,23,58),acctupdatetime=dt.datetime(2026,11,9,23,58));second=dict(first,acctoutputoctets=100,acctinputoctets=20,acctupdatetime=dt.datetime(2026,11,10,0,6))
            conn=connection();samples.capture_rows(conn,[first],dt.datetime(2026,1,1));samples.capture_rows(conn,[second],dt.datetime(2026,1,1));samples.capture_rows(conn,[second],dt.datetime(2026,1,1));conn.commit();conn.close()
            self.assertEqual(execute('SELECT COUNT(*) n FROM wisp_traffic_intervals',mode='one')['n'],1)
            daily=samples.daily_rows(self.query,days=1,now=NOW)[0];self.assertTrue(daily['unknown_download']);self.assertNotEqual(daily['download'],100)
            from services.measured_traffic_service import read_points
            points=read_points(self.query,dt.datetime(2026,11,9),dt.datetime(2026,11,11),{},None);self.assertEqual(sum(p['download'] or 0 for p in points),100)
            all_data=samples.series(self.query,dict(period='custom',date_from='2026-11-09',date_to='2026-11-10'),now=NOW);self.assertGreater(all_data['unknown_counters'],0)
            history=samples.series(self.query,dict(period='all'),now=NOW);self.assertEqual(history['labels'],['2026-11-09','2026-11-10'])
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Repair);result=unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'synthetic_only':True}),flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
