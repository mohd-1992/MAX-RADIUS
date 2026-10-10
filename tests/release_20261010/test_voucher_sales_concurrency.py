"""MariaDB integration: parallel activation, replayed Starts, renewal sales and migration.
Run only against an isolated, empty fixture database with cloned production tables.
SALES_REGRESSION_DATABASE must begin with max_sales_regression_.
"""
import os,sys,json,concurrent.futures
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from database.db import get_connection
from database.voucher_sales_schema import ensure_activation_sale_key
from database.schema_healer import TRIGGER_VOUCHER_SQL
DB=os.environ['SALES_REGRESSION_DATABASE']
assert DB.startswith('max_sales_regression_'), 'Use an isolated fixture database'
# Prepare cloned schema with UNUSED test cards, no sessions and no sale rows.
def conn():
 c=get_connection();c.select_db(DB);return c
c=conn()
with c.cursor() as x:
 x.execute('SELECT COUNT(*) n FROM wisp_voucher_sales');assert x.fetchone()['n']==0
 x.execute('SELECT COUNT(*) n FROM radacct');assert x.fetchone()['n']==0
ensure_activation_sale_key(c)
with c.cursor() as x:
 x.execute('DROP TRIGGER IF EXISTS trg_radacct_activate_voucher')
 x.execute(TRIGGER_VOUCHER_SQL)
 x.execute("SELECT id,username FROM wisp_vouchers ORDER BY id");cards=x.fetchall()
c.close()
def start(item):
 i,u,serial=item;c=conn()
 try:
  with c.cursor() as x:x.execute("INSERT INTO radacct(username,acctuniqueid,acctsessionid,nasipaddress,acctstarttime,acctupdatetime,acctinputoctets,acctoutputoctets,acctsessiontime) VALUES(%s,%s,%s,'10.78.0.8',NOW(),NOW(),0,0,0)",(u,'sales-regression-'+str(serial),'sale-'+str(serial)))
  c.commit();return dict(ok=True,id=i)
 except Exception as e:c.rollback();return dict(ok=False,id=i,error=str(e))
 finally:c.close()
# Distinct first activations at the same 64-client concurrency as the RADIUS ramp.
with concurrent.futures.ThreadPoolExecutor(64) as pool:results=list(pool.map(start,[(r['id'],r['username'],i) for i,r in enumerate(cards)]))
c=conn()
with c.cursor() as x:
 x.execute('SELECT COUNT(*) n FROM wisp_voucher_sales');first=x.fetchone()['n']
 # A retried Start for an already activated voucher must not duplicate its sale.
 x.execute("UPDATE wisp_vouchers SET status='unused' WHERE id=%s",(cards[0]['id'],))
c.commit();c.close()
with concurrent.futures.ThreadPoolExecutor(32) as pool:repeats=list(pool.map(start,[(cards[0]['id'],cards[0]['username'],10000+i) for i in range(32)]))
c=conn()
with c.cursor() as x:
 x.execute('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=%s',(cards[0]['id'],));repeat_sales=x.fetchone()['n']
 # Renewals deliberately omit the first-activation key and remain separate sales.
 for i in range(2):x.execute("INSERT INTO wisp_voucher_sales(voucher_id,username,price,package_name) VALUES(%s,%s,20,'renewal regression')",(cards[0]['id'],cards[0]['username']))
c.commit();ensure_activation_sale_key(c);ensure_activation_sale_key(c)
with c.cursor() as x:
 x.execute('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=%s',(cards[0]['id'],));with_renewals=x.fetchone()['n']
 x.execute('SELECT COUNT(*) n FROM wisp_voucher_sales WHERE voucher_id=%s AND first_activation_voucher_id IS NOT NULL',(cards[0]['id'],));keys=x.fetchone()['n']
 x.execute("SELECT COUNT(*) n FROM wisp_vouchers WHERE status='active'");active=x.fetchone()['n']
c.close()
out=dict(cards=len(cards),activation_failures=[r for r in results if not r['ok']],first_sales=first,active=active,repeat_failures=[r for r in repeats if not r['ok']],sales_after_repeated_starts=repeat_sales,sales_after_two_renewals=with_renewals,activation_keys=keys)
print(json.dumps(out));assert not out['activation_failures'] and first==len(cards) and not out['repeat_failures'] and repeat_sales==1 and with_renewals==3 and keys==1

# A surviving activation marker on a later historical row must not be moved.
c=conn()
with c.cursor() as x:
 x.execute('SELECT id FROM wisp_voucher_sales WHERE voucher_id=%s ORDER BY id DESC LIMIT 1',(cards[0]['id'],));last=x.fetchone()['id']
 x.execute('UPDATE wisp_voucher_sales SET first_activation_voucher_id=NULL WHERE voucher_id=%s',(cards[0]['id'],))
 x.execute('UPDATE wisp_voucher_sales SET first_activation_voucher_id=%s WHERE id=%s',(cards[0]['id'],last))
c.commit();ensure_activation_sale_key(c);ensure_activation_sale_key(c)
with c.cursor() as x:
 x.execute('SELECT id FROM wisp_voucher_sales WHERE first_activation_voucher_id=%s',(cards[0]['id'],));assert x.fetchone()['id']==last
c.close()
print(json.dumps({'existing_nonminimum_marker_preserved':True}))
