"""Explicitly bound incident operator. Never discovers credentials or connects.

The root host dispatcher must validate machine/container identities first. The
operator independently pins the DB endpoint, definitions, incident and plan.
Only timestamp UPDATEs and the separate recovery journal may be written.
"""
import copy
import hashlib
import re
import time

from lab_repair import Refused,canonical,digest
from priority_repair import PriorityLabRepair
import plan_codec

JOURNAL='max_clock_record_repair_20261017_journal'
INCIDENT='20261017-boot-clock-step'


class CustomerOperator(PriorityLabRepair):
    journal=JOURNAL

    def __init__(self,conn,binding,host_proof,*,lab=False):
        self.binding=copy.deepcopy(binding)
        self.host_proof=copy.deepcopy(host_proof)
        self.lab=lab
        self.current_plan=None
        self.scope_ids=set(binding['radacct_ids'])
        self.auth_ids=set(binding['auth_ids'])
        super().__init__(conn,binding['update_triggers'])

    def verify_target(self):
        b=self.binding
        if b.get('incident')!=INCIDENT or not b.get('authorized_after_local_tests'):
            raise Refused('Explicit incident authorization required')
        if b.get('expires_epoch',0)<time.time():raise Refused('Binding expired')
        for key in ('machine_sha256','web_id','db_id','core_id','web_image','core_image'):
            if not b.get(key) or self.host_proof.get(key)!=b[key]:raise Refused('Host/container binding mismatch')
        row=self.q('SELECT DATABASE() db,@@hostname server_host,@@server_id server_id')[0]
        if any(str(row[k])!=str(b[k]) for k in ('db','server_host','server_id')):
            raise Refused('Database endpoint binding mismatch')
        if not self.lab and str(row['db']).startswith('max_clock_repair_lab_'):
            raise Refused('Live binding cannot target a lab')
        if self.lab and not str(row['db']).startswith('max_clock_repair_lab_'):
            raise Refused('Lab binding cannot target customer')
        if len(self.scope_ids)!=50 or len(self.auth_ids)!=57:
            raise Refused('Unexpected frozen incident scope')

    def q(self,sql,params=()):
        text=' '.join(sql.split());upper=text.upper()
        allowed=upper.startswith(('SELECT ','SET TIME_ZONE=','SET SESSION ')) or upper=='START TRANSACTION READ ONLY'
        if upper.startswith('SET SESSION ') and 'GLOBAL' in upper:allowed=False
        if text=='UPDATE radacct SET acctstarttime=%s,acctupdatetime=%s,acctstoptime=%s WHERE radacctid=%s':
            allowed=bool(params and params[-1] in self.scope_ids)
        if text=='UPDATE radpostauth SET authdate=%s WHERE id=%s':
            allowed=bool(params and params[-1] in self.auth_ids)
        if text.startswith('INSERT INTO '+self.journal+'('):
            allowed=self.current_plan is not None
            if allowed:
                params=list(params);params[1]=plan_codec.dumps(self.current_plan)
        if text==f"UPDATE {self.journal} SET state='rolled_back' WHERE manifest=%s":allowed=True
        if not allowed:raise Refused('SQL outside timestamp/journal allowlist')
        return super().q(sql,params)

    @staticmethod
    def scrub(rows):
        result=copy.deepcopy(rows)
        for row in result:
            for key in ('pass','password','pin_code'):
                if key in row:
                    value=canonical(row[key]).encode()
                    row[key]='sha256:'+hashlib.sha256(value).hexdigest()
        return result

    def rows(self,table,key,ids,lock=False):
        return self.scrub(super().rows(table,key,ids,lock=lock))

    def context(self,cards):
        return {k:self.scrub(v) for k,v in super().context(cards).items()}

    def verify_definitions(self):
        b=self.binding
        if self.triggers()!=b['update_triggers']:raise Refused('UPDATE trigger changed')
        rows=self.q("SELECT TRIGGER_NAME name,SHA2(ACTION_STATEMENT,256) sha FROM information_schema.triggers WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='radacct'")
        if {r['name']:r['sha'] for r in rows}!=b['all_triggers']:raise Refused('Accounting writer definitions changed')
        rows=self.q("SELECT ROUTINE_NAME name,SHA2(ROUTINE_DEFINITION,256) sha FROM information_schema.routines WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME IN ('fn_freeze_voucher_policy','fn_check_license_auth','fn_reserve_license_slot')")
        if {r['name']:r['sha'] for r in rows}!=b['routines']:raise Refused('Authentication writer definitions changed')

    def ensure_journal(self):
        # One new unreferenced recovery table, never an ALTER of application tables.
        self.c.rollback()
        with self.c.cursor() as cur:
            cur.execute('SET SESSION lock_wait_timeout=2')
            cur.execute('CREATE TABLE IF NOT EXISTS '+self.journal+'(manifest CHAR(64) PRIMARY KEY,state VARCHAR(20),plan_json LONGTEXT,post_context_sha CHAR(64)) ENGINE=InnoDB')
        self.c.commit()
        cols=self.q('SELECT COLUMN_NAME n,COLUMN_TYPE t FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION',(self.journal,))
        if [(r['n'],r['t']) for r in cols]!=[('manifest','char(64)'),('state','varchar(20)'),('plan_json','longtext'),('post_context_sha','char(64)')]:
            raise Refused('Recovery journal schema mismatch')

    def prepare_bound(self,card_ids,*,acct_ids=None,auth_ids=None):
        self.verify_definitions()
        a=sorted(self.scope_ids if acct_ids is None else acct_ids)
        h=sorted(self.auth_ids if auth_ids is None else auth_ids)
        if not set(a)<=self.scope_ids or not set(h)<=self.auth_ids:raise Refused('Prepare expands frozen scope')
        plan=super().prepare(a,h,card_ids,-10743,'same-boot-journal-step-and-host-receipt-sql')
        plan.pop('sha256');plan['target_binding_sha']=digest(self.binding)
        plan['sha256']=digest(plan)
        return plan

    def execute(self,plan,*,rollback=False,checkpoint=None):
        if plan.get('target_binding_sha')!=digest(self.binding):raise Refused('Plan belongs to another bound run')
        if any(r['radacctid'] not in self.scope_ids for r in plan['acct']) or any(r['id'] not in self.auth_ids for r in plan['auth']):
            raise Refused('Plan expands frozen scope')
        self.verify_definitions()
        lease=self.q('SELECT is_valid ok,valid_until>UTC_TIMESTAMP() fresh FROM wisp_license_runtime_state WHERE id=1')
        if not lease or not lease[0]['ok'] or not lease[0]['fresh']:raise Refused('Fresh verified runtime lease required')
        self.current_plan=plan
        try:return super().execute(plan,rollback=rollback,checkpoint=checkpoint)
        finally:self.current_plan=None

    def receipt_plan(self,manifest):
        if not re.fullmatch('[a-f0-9]{64}',manifest):raise Refused('Invalid receipt identifier')
        rows=self.q('SELECT plan_json FROM '+self.journal+' WHERE manifest=%s',(manifest,))
        if not rows:raise Refused('Receipt not found')
        return plan_codec.loads(rows[0]['plan_json'])
