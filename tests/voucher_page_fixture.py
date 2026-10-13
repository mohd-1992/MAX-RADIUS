import ast,datetime,json,logging,os,re,secrets,sqlite3,string,sys,types,subprocess,copy
from pathlib import Path
from unittest.mock import patch
from html.parser import HTMLParser
from flask import request,session,jsonify,redirect,url_for,flash,render_template,Response
ROOT=Path(__file__).resolve().parents[1]
from test_package_page import fixture as package_fixture,load_functions
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from urllib.parse import urlencode

def voucher_fixture():
    app,db,ns=package_fixture()
    class Elements(HTMLParser):
        def __init__(self,html):super().__init__();self.items=[];self.feed(html)
        def handle_starttag(self,tag,attrs):self.items.append((tag,dict(attrs)))
    definitions={}
    def scan(sql):
        for match in re.finditer(r'CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)`?\s*\((.*?)\)\s*ENGINE',sql,re.S|re.I):
            cols=definitions.setdefault(match[1],{})
            for line in match[2].splitlines():
                c=re.match(r'\s*`(\w+)`\s+(\w+)',line)
                if c:cols[c[1]]=c[2]
    for file in ['init_db.sql','database/schema_mysql.sql']:scan((ROOT/file).read_text(encoding='utf-8'))
    tree=ast.parse((ROOT/'database/schema_healer.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ['REQUIRED_TABLES','REQUIRED_COLUMNS'] for t in node.targets):
            if isinstance(node.value,ast.Dict):
                data={ast.literal_eval(k):ast.literal_eval(v) for k,v in zip(node.value.keys,node.value.values) if isinstance(v,(ast.Constant,ast.List,ast.Tuple))}
            else:data=ast.literal_eval(node.value)
            for name,value in data.items():
                if isinstance(value,str):scan(value)
                else:
                    for col,kind in value:definitions.setdefault(name,{})[col]=kind.split()[0]
    design_tree=ast.parse((ROOT/'services/card_design_service.py').read_text(encoding='utf-8'))
    columns=next(n.value for n in ast.walk(design_tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='new_columns' for t in n.targets))
    for col,kind in ast.literal_eval(columns):definitions['wisp_card_templates'][col]=kind.split()[0]
    for name in ['wisp_audit_logs','wisp_packages','wisp_subscribers','wisp_vouchers','radacct','radcheck','radreply','radusergroup','wisp_voucher_batches','wisp_managers','wisp_roles','wisp_resellers','wisp_manager_invoices','wisp_reseller_transactions','wisp_card_templates','wisp_system_settings','wisp_voucher_sales','wisp_nas_devices','wisp_session_baselines','user_audit_logs']:
        db.execute('DROP TABLE IF EXISTS '+name)
        cols=definitions.get(name,{})
        assert cols,name
        fields=[f'`{col}` '+('INTEGER PRIMARY KEY' if col=='id' else 'NUMERIC' if kind.lower().startswith(('int','bigint','decimal','float','double','tinyint')) else 'TEXT') for col,kind in cols.items()]
        db.execute('CREATE TABLE '+name+'('+','.join(fields)+')')
    def rows(sql,params=()):return [dict(r) for r in db.execute(sql.replace('%s','?'),params)]
    def one(sql,params=()):
        result=rows(sql,params);return result[0] if result else None
    def write(sql,params=()):
        c=db.execute(sql.replace('%s','?'),params);db.commit();return c.lastrowid
    def many(sql,params):db.executemany(sql,params);db.commit()
    def seed(table,**values):write('INSERT INTO '+table+'('+','.join('`'+k+'`' for k in values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
    db.create_function('CONCAT',-1,lambda *a:''.join(str(v) for v in a))
    now=datetime.datetime(2026,10,11,18)
    db.create_function('NOW',0,lambda:now.isoformat(' '))
    ns.update(query_all=rows,query_one=one,execute_write=write,execute_many=many,execute_update=write,
        session=session,
        datetime=datetime,json=json,os=os,secrets=secrets,string=string,logging=logging,Response=Response,
        logger=logging.getLogger('audit'),get_db_storage_now=lambda:now,
        format_bytes=lambda x:f'{int(x or 0)} B',format_duration=lambda x:f'{x or 0} seconds',
        build_mikrotik_rate_limit=lambda **kw:'4M/2M',get_current_manager=lambda:session.get('manager'),
        get_subscriber_sessions=lambda *a,**kw:[],get_user_audit_logs=lambda *a:[],
        get_user_usage_analytics=lambda *a:dict(has_data=False,labels=[]),get_heartbeat_cutoff_str=lambda n=5:(now-datetime.timedelta(minutes=n)).isoformat(' '),
        sync_voucher_activations=lambda:None,log_audit=lambda *a:None,log_user_audit=lambda *a:None,
        ensure_card_designs_table=lambda:None)
    load_functions('services/voucher_service.py',['generate_random_code','generate_voucher_batch','get_batches','get_voucher_summary_counts','get_vouchers','get_batch_cards_for_print','update_voucher_card'],ns)
    design_tree=ast.parse((ROOT/'services/card_design_service.py').read_text(encoding='utf-8'))
    ns['DEFAULT_CARD_CONFIG']=ast.literal_eval(next(n.value for n in design_tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DEFAULT_CARD_CONFIG' for t in n.targets)))
    load_functions('services/card_design_service.py',['get_all_card_designs','get_card_design_by_id','save_card_design','delete_card_design','set_default_card_design','duplicate_card_design'],ns)
    ns['is_mysql_conn']=lambda conn:False
    class_node=next(n for n in ast.parse((ROOT/'services/voucher_service.py').read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='_VoucherWriter')
    exec(compile(ast.Module(body=[class_node],type_ignores=[]),'voucher-writer','exec'),ns)
    load_functions('core/radius_sync.py',['sync_voucher_to_radius'],ns)
    load_functions('services/quick_action_service.py',['parse_quota_adjustment','parse_entitlement_adjustment','action_adjust_entitlement','action_extend_time','action_add_quota','action_activate_subscriber'],ns)
    routes=['guard_voucher_permissions','api_disconnect_voucher','api_activate_voucher','vouchers','generate_vouchers_action','view_batch_details','edit_card_action','batches_bulk_action','active_card_users','active_card_user_details','edit_active_card_action','update_card_action','card_inspect','api_reset_voucher_mac','api_extend_voucher','voucher_designs_list','new_voucher_design','edit_voucher_design','save_card_design_api','print_cards']
    load_functions('web/routes/vouchers.py',routes,ns)
    for path,ep,methods in [('/api/vouchers/disconnect','api_disconnect_voucher',['POST']),('/api/vouchers/activate','api_activate_voucher',['POST']),('/vouchers','vouchers',['GET']),('/vouchers/generate','generate_vouchers_action',['POST']),('/vouchers/batch/<int:batch_id>','view_batch_details',['GET']),('/vouchers/batches/bulk-action','batches_bulk_action',['POST']),('/vouchers/active-users','active_card_users',['GET']),('/vouchers/active-users/<int:card_id>','active_card_user_details',['GET']),('/vouchers/active-users/edit/<int:card_id>','edit_active_card_action',['POST']),('/vouchers/update-card','update_card_action',['POST']),('/vouchers/inspect','card_inspect',['GET']),('/api/vouchers/reset-mac','api_reset_voucher_mac',['POST']),('/api/vouchers/extend-quota-time','api_extend_voucher',['POST']),('/vouchers/designs','voucher_designs_list',['GET']),('/vouchers/designs/new','new_voucher_design',['GET']),('/vouchers/designs/edit/<int:design_id>','edit_voucher_design',['GET']),('/api/vouchers/designs/save','save_card_design_api',['POST']),('/vouchers/print/<int:batch_id>','print_cards',['GET'])]:app.add_url_rule(path,ep,ns[ep],methods=methods)
    import importlib.util
    spec=importlib.util.spec_from_file_location('audit_csrf',ROOT/'core/license_security.py');security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security);security.install_csrf(app,security.protects_admin_account_request)
    app.jinja_env.filters['format_datetime']=str;app.jinja_env.filters['format_duration']=str
    seed('wisp_packages',id=1,name='Fixture package',price=10,cost=0,service_type='hotspot',is_active=1,rate_download='4M',rate_upload='2M',volume_quota_mb=10,uptime_limit_mins=0,validity_value=0,validity_unit='days',validity_days=0,simultaneous_sessions=1)
    seed('wisp_voucher_batches',id=1,name='Fixture batch',batch_number='FIX1',package_id=1,card_count=6,price=10,cost=0,volume_quota_mb=10,validity_value=0,validity_days=0,validity_unit='days',rate_download='4M',rate_upload='2M',pin_only=0)
    seed('wisp_managers',id=1,username='fixture-manager',wallet_balance=100,is_active=1,is_deleted=0)
    for ident,state in enumerate(['unused','active','expired','recharged','suspended','disabled'],1):
        seed('wisp_vouchers',id=ident,batch_id=1,package_id=1,username='fixture-'+str(ident),password='fixture-password',pin_code='fixture-pin-'+str(ident),serial_number='fixture-serial-'+str(ident),status=state,snap_price=10,snap_volume_quota_mb=10,snap_validity_value=0,snap_validity_unit='days',snap_validity_days=0,balance=100,extra_quota_mb=5,created_at='2026-10-01 12:00:00',last_renewed_at='2026-10-11 17:00:00',expires_at='2027-01-01 12:00:00' if state!='unused' else None)
        seed('radcheck',username='fixture-'+str(ident),attribute='Cleartext-Password',op=':=',value='fixture-password')
    seed('radacct',radacctid=1,username='fixture-2',acctsessionid='sess',nasipaddress='fixture-nas',acctinputoctets=100,acctoutputoctets=200,acctsessiontime=7200,acctstarttime='2026-10-11 16:00:00',acctupdatetime='2026-10-11 17:56:00')
    seed('wisp_card_templates',id=1,name='Fixture design',design_type='image',width_mm=85,height_mm=52,cards_per_row=2,cards_per_col=5,margin_top_mm=10,margin_page_mm=10,card_spacing_mm=3,is_default=1,config_json=json.dumps(ns['DEFAULT_CARD_CONFIG']),bg_image='')
    app.before_request(ns['guard_voucher_permissions'])
    ownership_stub=types.ModuleType('core.ownership');ownership_stub.generation_owner=lambda owner,package:owner
    license_stub=types.ModuleType('services.license_guard_service');license_stub.get_active_license_status=lambda:dict(valid=True,status='active')
    quota_stub=types.ModuleType('services.quota_service');quota_stub.calculate_account_quota=lambda *a,**kw:dict(used_down_bytes=150,used_up_bytes=75,used_total_bytes=225,is_quota_unlimited=False,total_quota_mb=15,remaining_bytes=15*1048576-225,quota_used_pct=1)
    sub_stub=types.ModuleType('services.subscriber_service');sub_stub.get_heartbeat_cutoff_str=ns['get_heartbeat_cutoff_str']
    time_stub=types.ModuleType('core.time_service');time_stub.get_utc_cutoff_str=lambda n=3:(now-datetime.timedelta(minutes=n)).isoformat(' ')
    quota_ns=dict(query_all=rows)
    load_functions('services/quota_service.py',['get_accounting_totals_batch','get_voucher_accounting_totals_batch'],quota_ns)
    class AccountingReadError(Exception):pass
    quota_ns['AccountingReadError']=AccountingReadError
    quota_stub.get_voucher_accounting_totals_batch=quota_ns['get_voucher_accounting_totals_batch']
    quota_stub.AccountingReadError=AccountingReadError
    ns['get_target_entity']=lambda kind,ident:(one('SELECT * FROM '+('wisp_vouchers' if kind=='voucher' else 'wisp_subscribers')+' WHERE id=?',(ident,)),kind)
    ns['Decimal']=Decimal;ns['InvalidOperation']=InvalidOperation;ns['ROUND_HALF_UP']=ROUND_HALF_UP;ns['urlencode']=urlencode
    radius_stub=types.ModuleType('core.radius_sync');radius_stub.sync_voucher_to_radius=ns['sync_voucher_to_radius'];radius_stub.sync_subscriber_to_radius=lambda *args,**kw:True
    lifecycle_stub=types.ModuleType('services.account_lifecycle_service')
    def lifecycle_sql(conn,statement,params=(),fetch=None):
        cur=conn.execute(statement.replace(' FOR UPDATE',''),params)
        if fetch=='one':
            row=cur.fetchone();return dict(row) if row else None
        return cur.rowcount
    lifecycle_ns=dict(sql=lifecycle_sql,Decimal=Decimal)
    load_functions('services/account_lifecycle_service.py',['expiry_reason'],lifecycle_ns)
    lifecycle_ns['ACCOUNTING_UNCERTAIN_REASON']='تعذر تسوية حدود دورة المحاسبة'
    lifecycle_stub.expiry_reason=lifecycle_ns['expiry_reason'];lifecycle_stub.ACCOUNTING_UNCERTAIN_REASON=lifecycle_ns['ACCOUNTING_UNCERTAIN_REASON']
    lifecycle_stub.sql=lifecycle_sql
    lifecycle_stub.lock_account=lambda conn,kind,ident:('wisp_vouchers',lifecycle_sql(conn,'SELECT *, CURRENT_TIMESTAMP checked_at FROM wisp_vouchers WHERE id=?',(ident,),'one'))
    def run_tx(operation):
        with ns['db_session']() as conn:return operation(conn)
    lifecycle_stub.run_transaction=run_tx
    stubs={'core.radius_sync':radius_stub,'services.account_lifecycle_service':lifecycle_stub,'services.license_guard_service':license_stub,'services.quota_service':quota_stub,'services.subscriber_service':sub_stub,'core.time_service':time_stub}

    from visibility_fixture import install_fixture_visibility
    install_fixture_visibility(app,ns)
    stubs['core.ownership']=ownership_stub
    return app,db,ns,stubs,seed
