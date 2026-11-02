"""Timestamp-only repair prototype. HARD REFUSES non-laboratory databases.

No credential loading, network connection, schema installer, or live entrypoint.
The offset is an evidenced clock normalization, not a recovered packet timestamp.
"""
import copy
import datetime as dt
import hashlib
import json


class Refused(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, default=str, ensure_ascii=True, sort_keys=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class LabRepair:
    journal = 'clock_repair_lab_journal'

    def verify_target(self):
        if not str(self.q('SELECT DATABASE() db')[0]['db']).startswith('max_clock_repair_lab_'):
            raise Refused('Laboratory database name required')

    def __init__(self, conn, trigger_hashes):
        self.c = conn
        self.trigger_hashes = trigger_hashes
        self.verify_target()
        self.q("SET time_zone='+03:00'")
        self.q('SET SESSION innodb_lock_wait_timeout=2')
        self.c.commit()

    def q(self, sql, params=()):
        with self.c.cursor() as cur:
            cur.execute(sql, params if params else None)
            return list(cur.fetchall()) if cur.description else cur.rowcount

    def rows(self, table, key, ids, lock=False):
        if not ids or len(ids) != len(set(ids)) or any(type(i) is not int or i < 1 for i in ids):
            raise Refused('Invalid frozen identifiers')
        if (table, key) not in {('radacct', 'radacctid'), ('radpostauth', 'id'), ('wisp_vouchers', 'id')}:
            raise Refused('Unsupported table')
        out = self.q(f'SELECT * FROM {table} WHERE {key} IN (' + ','.join(['%s']*len(ids)) +
                     f') ORDER BY {key}' + (' FOR UPDATE' if lock else ''), sorted(ids))
        if len(out) != len(ids):
            raise Refused('Missing frozen row')
        return out

    def triggers(self):
        return {r['name']: r['sha'] for r in self.q(
            "SELECT TRIGGER_NAME name,SHA2(ACTION_STATEMENT,256) sha FROM information_schema.triggers "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='radacct' AND EVENT_MANIPULATION='UPDATE'")}

    def guards(self, ids):
        for table in ('wisp_nas_session_seals', 'wisp_session_reservations'):
            # Index/range locks also prevent a seal/reservation appearing during the transaction.
            if self.q(f'SELECT radacctid FROM {table} WHERE radacctid IN (' +
                      ','.join(['%s']*len(ids)) + ') FOR UPDATE', ids):
                raise Refused('Target has a seal or reservation')

    def context(self, cards):
        names = [c['username'] for c in cards]
        placeholders = ','.join(['%s']*len(names))
        # Conservative rollback gate includes all accounting, baselines, sales and auth for these accounts.
        result = {}
        for table, key in (('radacct','radacctid'), ('radpostauth','id'),
                           ('wisp_session_baselines','radacctid'), ('wisp_voucher_sales','id')):
            if table == 'wisp_voucher_sales':
                result[table] = self.q('SELECT * FROM wisp_voucher_sales WHERE voucher_id IN (' +
                    ','.join(['%s']*len(cards)) + ') ORDER BY id', [c['id'] for c in cards])
            else:
                result[table] = self.q(f'SELECT * FROM {table} WHERE username IN ({placeholders}) ORDER BY {key}', names)
        return result

    @staticmethod
    def shifted(rows, fields, offset):
        result = copy.deepcopy(rows)
        for row in result:
            for field in fields:
                if row[field] is not None:
                    row[field] += dt.timedelta(seconds=offset)
        return result

    def prepare(self, acct_ids, auth_ids, card_ids, offset, evidence):
        if evidence != 'same-boot-journal-step-and-host-receipt-sql' or offset != -10743:
            raise Refused('Unapproved clock mapping')
        self.c.rollback()
        self.q('START TRANSACTION READ ONLY')
        try:
            a = self.rows('radacct','radacctid',acct_ids)
            h = self.rows('radpostauth','id',auth_ids)
            cards = self.rows('wisp_vouchers','id',card_ids)
            if self.triggers() != self.trigger_hashes:
                raise Refused('UPDATE trigger identity changed')
            names = {c['username'] for c in cards}
            if any(r['username'] not in names for r in a):
                raise Refused('Accounting outside frozen cards')
            for row in a:
                if row['acctstoptime'] is None:
                    raise Refused('Open session')
                times = [row[k] for k in ('acctstarttime','acctupdatetime','acctstoptime')]
                if any(t is None or not dt.datetime(2026,10,17,18,48) <= t <= dt.datetime(2026,10,17,18,49,32) for t in times):
                    raise Refused('Outside evidenced incident window')
                if times != sorted(times):
                    raise Refused('Nonchronological row')
                if any((row[k] or 0)<0 for k in ('acctinputoctets','acctoutputoctets','acctsessiontime')):
                    raise Refused('Negative counter')
                card = next(c for c in cards if c['username']==row['username'])
                cycle = card['last_renewed_at'] or card['first_used_at'] or card['created_at']
                if row['acctstarttime'] + dt.timedelta(seconds=offset) < cycle:
                    raise Refused('Would cross current cycle')
            if any(not dt.datetime(2026,10,17,18,48) <= r['authdate'] <= dt.datetime(2026,10,17,18,49,32) for r in h):
                raise Refused('Auth outside incident window')
            plan = {'acct':a,'auth':h,'cards':cards,'offset':offset,'evidence':evidence,
                    'trigger_hashes':self.trigger_hashes}
            plan['sha256']=digest(plan)
            return plan
        finally:
            self.c.rollback()

    def execute(self, plan, *, rollback=False, checkpoint=None):
        expected = dict(plan)
        sha = expected.pop('sha256')
        if digest(expected) != sha:
            raise Refused('Manifest changed')
        self.c.rollback()
        self.c.begin()
        try:
            # Deterministic card-first locks prevent concurrent renewal through the normal card lock.
            cards = self.rows('wisp_vouchers','id',[r['id'] for r in plan['cards']],True)
            if canonical(cards) != canonical(plan['cards']):
                raise Refused('Card contract/status/cycle changed')
            a = self.rows('radacct','radacctid',[r['radacctid'] for r in plan['acct']],True)
            h = self.rows('radpostauth','id',[r['id'] for r in plan['auth']],True)
            self.guards([r['radacctid'] for r in a])
            if self.triggers() != plan['trigger_hashes']:
                raise Refused('UPDATE trigger identity changed')
            journal = self.q(f'SELECT * FROM {self.journal} WHERE manifest=%s FOR UPDATE',(sha,))
            after_a = self.shifted(plan['acct'],('acctstarttime','acctupdatetime','acctstoptime'),plan['offset'])
            after_h = self.shifted(plan['auth'],('authdate',),plan['offset'])
            if journal and journal[0]['state'] == ('rolled_back' if rollback else 'applied'):
                ea,eh=(plan['acct'],plan['auth']) if rollback else (after_a,after_h)
                if canonical(a)!=canonical(ea) or canonical(h)!=canonical(eh):
                    raise Refused('Rows changed after previous operation')
                self.c.rollback()
                return 'already_done'
            if rollback and (not journal or journal[0]['state']!='applied'):
                raise Refused('No applied journal')
            if not rollback and journal:
                raise Refused('Completed manifest cannot be replayed')
            ea,eh = (after_a,after_h) if rollback else (plan['acct'],plan['auth'])
            if canonical(a)!=canonical(ea) or canonical(h)!=canonical(eh):
                raise Refused('Frozen accounting/auth changed')
            before_context = self.context(cards)
            if rollback and digest(before_context)!=journal[0]['post_context_sha']:
                raise Refused('New accounting/auth/baseline/sale prevents rollback')
            if checkpoint:
                checkpoint('locked')
            target_a,target_h = (plan['acct'],plan['auth']) if rollback else (after_a,after_h)
            for row in target_a:
                self.q('UPDATE radacct SET acctstarttime=%s,acctupdatetime=%s,acctstoptime=%s WHERE radacctid=%s',
                    [row['acctstarttime'],row['acctupdatetime'],row['acctstoptime'],row['radacctid']])
            if checkpoint:
                checkpoint('accounting_updated')
            for row in target_h:
                self.q('UPDATE radpostauth SET authdate=%s WHERE id=%s',[row['authdate'],row['id']])
            if canonical(self.rows('radacct','radacctid',[r['radacctid'] for r in a]))!=canonical(target_a):
                raise Refused('Trigger altered expected accounting result')
            if canonical(self.rows('radpostauth','id',[r['id'] for r in h]))!=canonical(target_h):
                raise Refused('Auth verification failed')
            if canonical(self.rows('wisp_vouchers','id',[r['id'] for r in cards]))!=canonical(cards):
                raise Refused('Card side effect')
            after_context = self.context(cards)
            # Except the explicit timestamp changes, every scoped row must remain byte-for-byte equivalent.
            normalized = copy.deepcopy(after_context)
            old_acct={r['radacctid']:r for r in before_context['radacct']}
            old_auth={r['id']:r for r in before_context['radpostauth']}
            for row in normalized['radacct']:
                if row['radacctid'] in {x['radacctid'] for x in a}:
                    for field in ('acctstarttime','acctupdatetime','acctstoptime'):
                        row[field]=old_acct[row['radacctid']][field]
            for row in normalized['radpostauth']:
                if row['id'] in {x['id'] for x in h}:
                    row['authdate']=old_auth[row['id']]['authdate']
            if canonical(normalized)!=canonical(before_context):
                raise Refused('Unexpected context side effect')
            if rollback:
                self.q(f"UPDATE {self.journal} SET state='rolled_back' WHERE manifest=%s",(sha,))
            else:
                self.q(f"INSERT INTO {self.journal}(manifest,state,plan_json,post_context_sha) VALUES(%s,'applied',%s,%s)",
                       (sha,canonical(plan),digest(after_context)))
            if checkpoint:
                checkpoint('before_commit')
            self.c.commit()
            return 'rolled_back' if rollback else 'applied'
        except BaseException:
            self.c.rollback()
            raise
