"""Private stdin JSON kernel, loaded in memory in the pinned runtime container.

No imports of the application, workers, issuer, NAS or customer access helpers.
"""
import json
import os
import time

import pymysql
from customer_operator import CustomerOperator
from lab_repair import Refused,digest
import plan_codec


def run(request):
    b=request['binding'];proof=request['host_proof']
    c=pymysql.connect(host=request['db_host'],port=int(request['db_port']),user='root',
        password=request['root_password'],database=b['db'],charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,autocommit=False,read_timeout=3,write_timeout=3,connect_timeout=3)
    try:
        e=CustomerOperator(c,b,proof,lab=bool(request.get('lab')))
        e.q('SET SESSION idle_transaction_timeout=1')
        action=request['action']
        if action=='prepare':
            e.verify_definitions()
            a=e.rows('radacct','radacctid',sorted(e.scope_ids))
            h=e.rows('radpostauth','id',sorted(e.auth_ids))
            names=sorted({r['username'] for r in a})
            cards=e.q('SELECT id,username,status FROM wisp_vouchers WHERE username IN ('+','.join(['%s']*len(names))+') ORDER BY id',names)
            if len(cards)!=50:raise Refused('Frozen rows no longer map to 50 existing cards')
            orphans=[r for r in h if r['username'] not in names]
            if len(orphans)!=7 or len({r['username'] for r in orphans})!=1:
                raise Refused('Internal auth scope changed')
            if digest(orphans[0]['username'])!=b['internal_auth_user_sha']:
                raise Refused('Internal healthcheck identity changed')
            plans=[];deferred=[];orphan_ids=[r['id'] for r in orphans]
            for card in cards:
                acct=[r['radacctid'] for r in a if r['username']==card['username']]
                auth=[r['id'] for r in h if r['username']==card['username']]
                if not plans:auth+=orphan_ids
                try:
                    plan=e.prepare_bound([card['id']],acct_ids=acct,auth_ids=auth)
                    # Same renewal-cycle inclusion before and after is already enforced; baseline/usage check below.
                    rows=e.q('SELECT COUNT(*) n FROM radacct WHERE username=%s AND acctstoptime IS NULL',(card['username'],))
                    plans.append({'plan':plan_codec.dumps(plan),'manifest':plan['sha256'],'open_sessions':rows[0]['n'],
                                  'status':card['status'],'card_id':card['id']})
                except Refused as exc:deferred.append({'card_id':card['id'],'reason':str(exc)})
            pilot=next((p['manifest'] for p in plans if p['open_sessions']==0 and p['status']=='expired'),None)
            if not pilot:pilot=next((p['manifest'] for p in plans if p['open_sessions']==0),None)
            if not pilot:raise Refused('No quiet pilot card')
            return {'ok':True,'plans':plans,'deferred':deferred,'pilot':pilot,
                    'summary':{'prepared_cards':len(plans),'deferred_cards':len(deferred),'pilot_has_no_open_sql':True,'data_mutation':False}}
        if action=='journal':
            e.verify_definitions();e.ensure_journal()
            return {'ok':True,'summary':{'recovery_journal_ready':True,'application_tables_altered':False}}
        if action in ('apply','rollback'):
            results=[]
            for entry in request['plans']:
                plan=plan_codec.loads(entry['plan'])
                if entry['manifest']!=plan['sha256']:raise Refused('Selected manifest mismatch')
                if action=='rollback':
                    # Recover from the transactional journal, independently of the SSH client copy.
                    plan=e.receipt_plan(entry['manifest'])
                try:
                    result=e.execute(plan,rollback=action=='rollback')
                    results.append({'manifest':plan['sha256'],'status':result})
                except Refused as exc:
                    return {'ok':False,'partial':results,'deferred':plan['sha256'],'reason':str(exc),'automatic_retry':False}
                time.sleep(.02)
            return {'ok':True,'results':results,'summary':{'cards':len(results),'action':action,'automatic_retry':False}}
        if action=='status':
            rows=e.q('SELECT manifest,state FROM '+e.journal+' ORDER BY manifest')
            return {'ok':True,'receipts':rows}
        raise Refused('Unknown incident action')
    finally:
        try:c.rollback()
        finally:c.close()


def main():
    try:result=run(json.load(__import__('sys').stdin))
    except Refused as exc:result={'ok':False,'refused':True,'reason':str(exc)}
    except Exception as exc:result={'ok':False,'error_type':type(exc).__name__,'error_code':exc.args[0] if exc.args and isinstance(exc.args[0],int) else None}
    print(json.dumps(result,separators=(',',':')))
