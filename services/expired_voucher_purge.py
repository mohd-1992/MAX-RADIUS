"""Permanent expired-voucher deletion inside the caller's account transaction.

Financial rows and explicit usage summaries survive. Session payloads do not.
Unknown/unsettled history defers the whole account rather than partially deleting.
"""
import hashlib
import json
import re
from collections import defaultdict

from services.account_lifecycle_service import sql
from services.backup_usage_summary import COLS
from services.backup_compaction import UNCERTAIN
from services.deletion_history_policy import historical_observation_allowed

STORES = ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history')


def scope(source):
    name='COALESCE(account_username,username)' if source=='wisp_traffic_history' else 'account_read_username'
    return f'(account_voucher_id=? OR (account_voucher_id IS NULL AND account_subscriber_id IS NULL AND {name}=? AND BINARY {name}=BINARY ?))'


def detail_predicates(source, account):
    """Disjoint indexed branches, equivalent to scope; no COALESCE/OR scan."""
    if source not in STORES:
        raise ValueError('Invalid session store')
    name=account['username']
    branches=[('account_voucher_id=?',(account['id'],))]
    prefix='account_voucher_id IS NULL AND account_subscriber_id IS NULL AND '
    if source=='wisp_traffic_history':
        branches.extend(((prefix+'account_username=? AND BINARY account_username=BINARY ?',(name,name)),
                         (prefix+'account_username IS NULL AND username=? AND BINARY username=BINARY ?',(name,name))))
    else:
        branches.append((prefix+'account_read_username=? AND BINARY account_read_username=BINARY ?',(name,name)))
    return branches


def locked_details(conn, source, account, limit=5001):
    rows=[]
    for predicate,args in detail_predicates(source,account):
        remaining=limit-len(rows)
        if remaining<=0:break
        rows.extend(sql(conn,'SELECT * FROM '+source+' WHERE '+predicate+
                        ' LIMIT '+str(remaining)+' FOR UPDATE',args,'all'))
    return rows


def purge_details(conn, account, *, history_only=False, cutoff=None):
    """Returns a deferred result before any write, or a fully verified purge receipt."""
    vid=account['id'];sessions={};qualities={};ids={};removed={}
    if history_only and account.get('status')!='expired':
        return dict(deferred=True,reason='account_not_expired')
    aliases={r['username'] for r in sql(conn,'SELECT username FROM wisp_voucher_names WHERE voucher_id=?',(vid,),'all')}
    aliases.add(account['username'])
    names=tuple(sorted(aliases));name_marks=','.join('?' for _ in names)
    if (sql(conn,'SELECT id FROM wisp_subscribers WHERE username IN ('+name_marks+') LIMIT 1',names,'one') or
        sql(conn,'SELECT id FROM wisp_vouchers WHERE id<>? AND username IN ('+name_marks+') LIMIT 1',(vid,*names),'one')):
        return dict(deferred=True,reason='ambiguous_owner')
    inspected=0
    for source in STORES:
        # Bound Python payload memory even for an anomalously large single card.
        # The whole card is deferred, never partially purged or misreported as done.
        rows=locked_details(conn,source,account)
        if history_only and source!='radacct' and rows:
            # Existing quota readers count physical RADIUS rows + summaries.
            # Moving unique imported/archive payloads into that summary would
            # change an existing account's quota. Keep that entire card intact.
            return dict(deferred=True,reason='history_outside_live_quota')
        inspected+=len(rows)
        if inspected>5000:
            return dict(deferred=True,reason='large_history_requires_batch_purge')
        ids[source]=[r['history_id'] if source=='wisp_traffic_history' else r['radacctid'] for r in rows]
        if history_only and source=='radacct' and rows:
            for offset in range(0,len(ids[source]),250):
                batch=ids[source][offset:offset+250]
                if sql(conn,'SELECT radacctid FROM wisp_session_baselines WHERE radacctid IN ('+
                       ','.join('?' for _ in batch)+') LIMIT 1',tuple(batch),'one'):
                    return dict(deferred=True,reason='renewal_baseline')
        # Fetch seals in bounded sets rather than one network round trip/session.
        known_seals={}
        uids=list({r.get('acctuniqueid') for r in rows if r.get('acctuniqueid')})
        for offset in range(0,len(uids),250):
            chunk=uids[offset:offset+250]
            if history_only:
                for other in STORES[1:]:
                    if sql(conn,'SELECT 1 n FROM '+other+' WHERE acctuniqueid IN ('+
                           ','.join('?' for _ in chunk)+') LIMIT 1',tuple(chunk),'one'):
                        return dict(deferred=True,reason='copied_session_requires_review')
            found=sql(conn,'SELECT * FROM wisp_account_usage_seals WHERE acctuniqueid IN ('+
                      ','.join('?' for _ in chunk)+')',tuple(chunk),'all')
            known_seals.update((r['acctuniqueid'],r) for r in found)
        for row in rows:
            reason=(row.get('acctterminatecause') or '').lower().encode()
            observed_only=not row.get('acctstoptime') or reason in UNCERTAIN
            if history_only and (observed_only or not cutoff or row['acctstoptime']>=cutoff):
                return dict(deferred=True,reason='unsettled_or_recent_history')
            cycle=account.get('last_renewed_at')
            if history_only:cycle=cycle or account.get('first_used_at') or account.get('created_at')
            if history_only and (not re.fullmatch('[0-9a-fA-F]{32}',str(row.get('acctuniqueid') or '')) or
                not row.get('acctstarttime') or row['acctstoptime']<row['acctstarttime'] or
                (cycle and row['acctstarttime']<cycle<row['acctstoptime'])):
                return dict(deferred=True,reason='unsettled_or_incomplete_history')
            if (not row.get('acctstarttime') or
                (observed_only and not historical_observation_allowed(source,row,account)) or
                not row.get('acctuniqueid') or not row.get('acctsessionid') or
                any(row.get(k) is None or int(row[k])<0 for k in ('acctinputoctets','acctoutputoctets','acctsessiontime'))):
                return dict(deferred=True,reason='unsettled_or_incomplete_history')
            if row.get('account_subscriber_id') is not None:
                return dict(deferred=True,reason='ambiguous_owner')
            # A known summary seal already owns its counters. New late traffic must
            # be reconciled, not silently added or removed a second time.
            seal=known_seals.get(row['acctuniqueid'])
            if seal:
                if int(seal['voucher_id'])!=int(vid):
                    return dict(deferred=True,reason='ambiguous_owner')
                if any(int(row[k])>int(seal[j]) for k,j in (('acctinputoctets','input_bytes'),('acctoutputoctets','output_bytes'),('acctsessiontime','uptime_seconds'))):
                    return dict(deferred=True,reason='late_summary_delta')
                continue
            key=(row['acctuniqueid'],row['username'],row['acctsessionid'],row['nasipaddress'],str(row['acctstarttime']))
            qualities[key]=max(qualities.get(key,0),int(observed_only))
            if key in sessions:
                old=sessions[key]
                for k in ('acctinputoctets','acctoutputoctets','acctsessiontime'):old[k]=max(int(old[k]),int(row[k]))
            else:sessions[key]=row
    groups={};seals={};quality_counts={}
    for session_key,r in sessions.items():
        cycle=account.get('last_renewed_at');start=r['acctstarttime']
        if history_only:cycle=cycle or account.get('first_used_at') or account.get('created_at')
        scope_name='current' if not cycle or start>=cycle else 'previous'
        dims=tuple(r.get(k) or '' for k in ('nasipaddress','calledstationid','nasportid','framedprotocol','servicetype'))
        visible=int(r.get('acctterminatecause')!='Consolidated-Historical-Import')
        digest=hashlib.sha256(json.dumps([visible,vid,str(cycle) if cycle else None,scope_name,str(start)[:10],*dims],ensure_ascii=False).encode()).hexdigest()
        values=groups.setdefault(digest,[digest,vid,account['username'],account.get('reseller_id'),cycle,start,start,*dims,visible,0,0,0,0])
        values[5]=min(values[5],start);values[6]=max(values[6],start)
        for i,k in enumerate(('acctinputoctets','acctoutputoctets','acctsessiontime')):values[13+i]+=int(r[k])
        values[16]+=1
        quality_counts[digest]=quality_counts.get(digest,0)+qualities[session_key]
        previous=seals.get(r['acctuniqueid'])
        if previous is not None:
            return dict(deferred=True,reason='conflicting_session_identity')
        seals[r['acctuniqueid']]=(vid,int(r['acctinputoctets']),int(r['acctoutputoctets']),int(r['acctsessiontime']))
    marks=','.join('?' for _ in COLS)
    update=','.join(f'{k}={k}+VALUES({k})' for k in COLS[-4:])
    for values in groups.values():
        sql(conn,'INSERT INTO wisp_account_usage_summary ('+','.join(COLS)+',unconfirmed_sessions) VALUES ('+marks+',?) ON DUPLICATE KEY UPDATE '+update+',unconfirmed_sessions=unconfirmed_sessions+VALUES(unconfirmed_sessions),first_start=LEAST(first_start,VALUES(first_start)),last_start=GREATEST(last_start,VALUES(last_start))',tuple(values)+(quality_counts[values[0]],))
    if seals:
        from database.db import adapt_query
        with conn.cursor() as cur:
            cur.executemany(adapt_query('INSERT INTO wisp_account_usage_seals VALUES(?,?,?,?,?)',conn),[(uid,*values) for uid,values in seals.items()])
    for source in STORES:
        removed[source]=0
        primary='history_id' if source=='wisp_traffic_history' else 'radacctid'
        for offset in range(0,len(ids[source]),250):
            chunk=ids[source][offset:offset+250]
            removed[source]+=sql(conn,'DELETE FROM '+source+' WHERE '+primary+' IN ('+
                                 ','.join('?' for _ in chunk)+')',tuple(chunk))
        if removed[source]!=len(ids[source]):raise RuntimeError('Session purge verification failed')
        if locked_details(conn,source,account,limit=1):raise RuntimeError('Session purge left unexpected details')
    # No session-level MAC/IP/auth payloads survive. Identity aliases and compact
    # UID seals intentionally remain to protect historical identity/replay.
    if not history_only:
        for table in ('radpostauth','wisp_session_reservations','wisp_renewal_guards','wisp_online_policy_outbox'):
            removed[table]=sql(conn,'DELETE FROM '+table+' WHERE username IN ('+name_marks+')',names)
        for table in ('wisp_account_mac_addresses','wisp_account_mac_policies','wisp_account_mac_receipts'):
            removed[table]=sql(conn,"DELETE FROM "+table+" WHERE account_type='voucher' AND account_id=?",(vid,))
        sql(conn,'DELETE FROM wisp_voucher_edit_receipts WHERE voucher_id=?',(vid,))
    # Baselines point to physical radacct IDs; all corresponding details are gone.
    if ids['radacct']:
        for offset in range(0,len(ids['radacct']),250):
            batch=ids['radacct'][offset:offset+250]
            if not history_only:
                sql(conn,'DELETE FROM wisp_session_baselines WHERE radacctid IN ('+','.join('?' for _ in batch)+')',tuple(batch))
            sql(conn,'DELETE FROM wisp_traffic_checkpoints WHERE radacctid IN ('+','.join('?' for _ in batch)+')',tuple(batch))
    return dict(deferred=False,removed=removed,summarized_sessions=len(sessions),summary_groups=len(groups),unconfirmed_sessions=sum(qualities.values()))
