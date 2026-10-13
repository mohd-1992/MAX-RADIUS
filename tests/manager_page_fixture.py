import hashlib
from decimal import Decimal, InvalidOperation
import ast,sys,json,datetime,secrets,sqlite3,types,re,copy
from pathlib import Path
from contextlib import contextmanager
from functools import wraps
from html.parser import HTMLParser
from flask import Flask,request,session,jsonify,redirect,url_for,flash,render_template
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from voucher_page_fixture import voucher_fixture
def load(path,ns,names=None,nested=False):
 tree=ast.parse((ROOT/path).read_text(encoding='utf8'));nodes=[copy.deepcopy(n) for n in (ast.walk(tree) if nested else tree.body) if isinstance(n,ast.FunctionDef) and (names is None or n.name in names)]
 for n in nodes:n.decorator_list=[]
 exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),ns)
def fixture():
 app,db,ns,stubs,seed=voucher_fixture()
 app.before_request_funcs[None]=[]
 db.executescript('CREATE TABLE wisp_permissions(id INTEGER PRIMARY KEY,code TEXT,category TEXT,name TEXT); CREATE TABLE wisp_role_permissions(role_id INTEGER,permission_id INTEGER); CREATE TABLE wisp_admins(id INTEGER PRIMARY KEY,username TEXT,password TEXT,full_name TEXT,role TEXT,is_active INTEGER); CREATE TABLE wisp_reseller_wallets(reseller_id INTEGER PRIMARY KEY,balance REAL,credit_limit REAL,total_deposits REAL,total_purchases REAL,status TEXT); CREATE TABLE wisp_wallet_ledger(id INTEGER PRIMARY KEY,reseller_id INTEGER,trx_type TEXT,amount REAL,balance_after REAL,counterpart_reseller_id INTEGER,notes TEXT,created_at TEXT);')
 db.execute('ALTER TABLE wisp_reseller_wallets ADD COLUMN id INTEGER')
 db.execute('DELETE FROM wisp_managers')
 for ident in (1,2,3):
  seed('wisp_roles',id=ident,name='Role '+str(ident),code='superadmin' if ident==1 else 'restricted'+str(ident),is_system=1 if ident==1 else 0,description='Audit role')
  seed('wisp_managers',id=ident,username='audit'+str(ident),password_hash='pbkdf2:AUDIT-HASH',full_name='Audit '+str(ident),role_id=ident,wallet_balance=100,credit_limit=50,commission_percent=10,allowed_packages='[1]',is_active=1,is_deleted=0,phone='',email='',notes='')
  seed('wisp_admins',id=ident,username='audit'+str(ident),password='HASH',is_active=1)
  seed('wisp_resellers',id=ident,name='Legacy '+str(ident),phone='',contact_person='',balance=500,status='active')
  db.execute('INSERT INTO wisp_reseller_wallets VALUES(?,100,50,0,0,?,?)',(ident,'frozen' if ident==2 else 'active',ident))
 for ident,code in enumerate(['managers.view','managers.manage','managers.billing','roles.manage'],1):
  db.execute('INSERT INTO wisp_permissions VALUES(?,?,?,?)',(ident,code,'Audit',code))
 db.commit()
 class Cursor:
  def __enter__(self):return self
  def __exit__(self,*a):pass
  def execute(self,sql,args=()):
   sql=sql.replace('%s','?').replace(' FOR UPDATE','')
   if 'ON DUPLICATE KEY UPDATE' in sql: sql=sql.split('ON DUPLICATE KEY UPDATE')[0]
   self.cur=db.execute(sql,args);return self.cur
  def fetchone(self):
   r=self.cur.fetchone();return dict(r) if r else None
  def fetchall(self):return [dict(r) for r in self.cur.fetchall()]
  def close(self):pass
  @property
  def lastrowid(self):return self.cur.lastrowid
  @property
  def rowcount(self):return self.cur.rowcount
  def executemany(self,sql,args):self.cur=db.executemany(sql.replace('%s','?'),args)
 class Conn:
  def cursor(self):return Cursor()
  def commit(self):db.commit()
  def rollback(self):db.rollback()
  def close(self):pass
 @contextmanager
 def tx():
  try:yield Conn();db.commit()
  except Exception:db.rollback();raise
 ns.update(hashlib=hashlib,re=re,Decimal=Decimal,InvalidOperation=InvalidOperation,datetime=datetime,secrets=secrets,json=json,session=session,wraps=wraps,log_audit=lambda *a:None,db_session=tx,adapt_query=lambda s,c:s,hash_manager_password=lambda p:'HASH',verify_manager_password=lambda *a:True)
 load('services/manager_service.py',ns)
 wallet=dict(get_all_managers=ns['get_all_managers'],get_all_manager_invoices=ns['get_all_manager_invoices'],deposit_manager_wallet=ns['deposit_manager_wallet'],transfer_manager_wallet=ns['transfer_manager_wallet'])
 load('services/reseller_wallet_service.py',wallet,['transfer_reseller_balance','deposit_reseller_balance','get_wallets_overview'])
 wm=types.ModuleType('services.reseller_wallet_service');wm.transfer_reseller_balance=wallet['transfer_reseller_balance'];wm.deposit_reseller_balance=wallet['deposit_reseller_balance'];wm.get_wallets_overview=wallet['get_wallets_overview']
 stubs['services.reseller_wallet_service']=wm
 load('core/rbac.py',ns,['get_current_manager'])
 ns['has_permission']=lambda code,manager=None:code in session.get('permissions',[])
 load('core/rbac.py',ns,['require_permission'])
 load('web/routes/resellers.py',ns)
 tree=ast.parse((ROOT/'web/routes/resellers.py').read_text(encoding='utf8'))
 for n in tree.body:
  if not isinstance(n,ast.FunctionDef):continue
  route=next((d for d in n.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr=='route'),None)
  if not route:continue
  opts={k.arg:ast.literal_eval(k.value) for k in route.keywords};func=ns[n.name]
  perm=next((d for d in n.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Name) and d.func.id=='require_permission'),None)
  if perm:func=ns['require_permission'](ast.literal_eval(perm.args[0]))(func)
  app.add_url_rule(ast.literal_eval(route.args[0]),opts.get('endpoint',n.name),func,methods=opts.get('methods',['GET']))
 load('web/routes/auth.py',ns,['impersonate_exit','impersonate_manager'])
 app.add_url_rule('/impersonate/exit','impersonate_exit',ns['impersonate_exit'],methods=['POST'])
 app.add_url_rule('/managers/<int:manager_id>/impersonate','impersonate_manager',ns['impersonate_manager'],methods=['POST'])
 app.add_url_rule('/dashboard','dashboard',lambda:'ok');app.add_url_rule('/login','login',lambda:'login')
 ns['get_active_license_status']=lambda:{'valid':True}
 load('web/__init__.py',ns,['check_authentication'],nested=True)
 app.before_request(ns['check_authentication'])
 import importlib.util
 spec=importlib.util.spec_from_file_location('audit_security',ROOT/'core/license_security.py');security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security);security.install_csrf(app,security.protects_admin_account_request)
 db.execute('CREATE UNIQUE INDEX fixture_invoice_reference ON wisp_manager_invoices(invoice_number)');db.commit()
 app.jinja_env.filters['format_datetime']=str
 @app.context_processor
 def ctx():return {'current_manager':ns['get_current_manager'](),'has_permission':ns['has_permission'],'currency':'YER'}
 from visibility_fixture import install_fixture_visibility
 install_fixture_visibility(app,ns)
 return app,db,ns,stubs,seed,wallet
