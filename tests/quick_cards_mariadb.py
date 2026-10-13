"""Quick issuance concurrency against an explicitly isolated MariaDB schema."""
from pathlib import Path
# Reuse the ownership fixture setup only; never replay its operation round.
setup=Path(__file__).with_name('reseller_ownership_mariadb.py').read_text(encoding='utf-8-sig').split("with patch.dict(sys.modules,")[0]
exec(compile(setup,'ownership_fixture_setup','exec'))
from decimal import Decimal
sys.path.insert(0,'/app')
from database.quick_card_schema import install_quick_card_schema
with transaction() as conn:install_quick_card_schema(conn);install_quick_card_schema(conn)
quick=dict(json=json,re=re,Decimal=Decimal,db_session=transaction,is_mysql_conn=lambda c:True,raw_one=lambda s,p=():read(s,p,True),
 owner_id=own['owner_id'],generation_owner=own['generation_owner'],scoped_query_all=own['scoped_query_all'],scoped_query_one=own['scoped_query_one'],
 get_db_storage_now=ns['get_db_storage_now'],_VoucherWriter=ns['_VoucherWriter'],generate_voucher_batch=ns['generate_voucher_batch'])
load('services/quick_card_service.py',quick)
quick_mod.get_issue_policy=quick['get_issue_policy']
owner_mod.__dict__.update(own)
def issue(key,count=1,ident=2):
 with actor(ident):
  return quick['issue_quick_cards'](own['ownership_manager'](),1,count,key)
with patch.dict(sys.modules,{'services.license_guard_service':license_mod,'core.ownership':owner_mod,'services.quick_card_service':quick_mod}):
 write("INSERT INTO wisp_roles(id,name,code) VALUES(1,'admin','superadmin'),(2,'reseller','reseller')")
 write("INSERT INTO wisp_managers(id,username,password_hash,full_name,role_id,wallet_balance,is_active,is_deleted,allowed_packages) VALUES(1,'fixture-admin','hash','admin',1,100,1,0,NULL),(2,'fixture-a','hash','a',2,100,1,0,'[1]'),(3,'fixture-b','hash','b',2,100,1,0,'[1]')")
 write("INSERT INTO wisp_packages(id,name,price,cost,rate_download,rate_upload,service_type,is_active) VALUES(1,'fixture',10,8,'4M','2M','hotspot',1)")
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:issue('concurrent_same_request_01',2),range(2)))
 assert results[0]['id']==results[1]['id'] and balance(2)==84
 assert read('SELECT COUNT(*) n FROM wisp_manager_invoices',one=True)['n']==1
 assert read('SELECT COUNT(*) n FROM wisp_vouchers',one=True)['n']==2
 assert read('SELECT COUNT(*) n FROM radcheck',one=True)['n']==2
 assert read('SELECT COUNT(*) n FROM radusergroup',one=True)['n']==2
 checks.append('concurrent_identical_reference_one_batch_one_debit')
 write('UPDATE wisp_managers SET wallet_balance=8 WHERE id=2')
 def attempt(n):
  try:issue('concurrent_distinct_request_'+str(n));return True
  except ValueError:return False
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
 assert results.count(True)==1 and balance(2)==0;checks.append('distinct_requests_cannot_overspend')
 write('UPDATE wisp_managers SET wallet_balance=100 WHERE id=2')
 before=read('SELECT COUNT(*) n FROM wisp_vouchers',one=True)['n']
 financial_before={table:read('SELECT COUNT(*) n FROM '+table,one=True)['n'] for table in ('radcheck','radusergroup','wisp_manager_invoices','wisp_voucher_batches')}
 write("CREATE TRIGGER fail_quick_receipt BEFORE INSERT ON wisp_quick_card_issues FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='fixture receipt failure'")
 try:issue('receipt_failure_request_01')
 except pymysql.MySQLError:pass
 else:raise AssertionError('receipt failure accepted')
 finally:write('DROP TRIGGER fail_quick_receipt')
 assert balance(2)==100 and read('SELECT COUNT(*) n FROM wisp_vouchers',one=True)['n']==before
 assert all(read('SELECT COUNT(*) n FROM '+table,one=True)['n']==number for table,number in financial_before.items())
 checks.append('receipt_failure_rolls_back_cards_radius_wallet_invoice')
 with actor(1):quick['save_issue_policy'](dict(prefix='AB',char_type='alphanumeric',total_length=10,format_type='same_user_pass',password_length=8))
 new=issue('policy_request_00001')
 card=read('SELECT username,password FROM wisp_vouchers WHERE batch_id=?',(new['batch_id'],),True)
 assert len(card['username'])==10 and card['username'].startswith('AB') and card['password']==card['username'];checks.append('global_policy_applied_in_real_sql')
 with actor(2):
  quick['record_quick_print'](new['id']);quick['record_quick_print'](new['id'])
  assert quick['get_quick_issue'](new['id'])['print_requests']==2
 assert balance(2)==92;checks.append('reprints_do_not_debit_or_generate')
 with actor(3):assert quick['get_quick_issue'](new['id']) is None
 checks.append('receipt_owner_scope')
print(json.dumps({'isolated':True,'checks':checks,'passed':len(checks)}))
