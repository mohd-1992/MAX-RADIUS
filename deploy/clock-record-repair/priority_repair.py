"""Laboratory-only, yielding timestamp repair. No schema or global changes.

READ COMMITTED belongs only to the dedicated correction connection. Unmatched
reservation records and gaps are not retained by the unchanged UPDATE trigger.
The existing normal INSERT link trigger can link a reservation only to the new
radacct primary key, never one of the locked, already-closed historical rows.
This is not protection against an administrator bypassing those writer rules.
"""
import copy
import time

from lab_repair import LabRepair, Refused, digest


class Deferred(Refused):
    """Correction yielded; traffic/renewal wins. Do not automatically retry."""


class PriorityLabRepair(LabRepair):
    def __init__(self, conn, trigger_hashes, *, max_transaction_ms=250, statement_ms=50, pause_between_cards_ms=20):
        self.running = False
        self.checking = False
        self.started = None
        self.budget = max_transaction_ms / 1000
        self.timings = []
        self.last_waiter_check = 0
        if not 0 <= pause_between_cards_ms <= 100:
            raise Refused('Invalid pacing')
        self.pause=pause_between_cards_ms/1000
        super().__init__(conn, trigger_hashes)
        if not 20 <= max_transaction_ms <= 500 or not 5 <= statement_ms <= 100:
            raise Refused('Laboratory timing bounds required')
        self.q('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
        self.q('SET SESSION max_statement_time=%s', (statement_ms/1000,))
        self.c.commit()

    def check_priority(self, *, force=False):
        if not self.running or self.checking:
            return
        now=time.monotonic()
        if now-self.started > self.budget:
            raise Deferred('Correction transaction budget exceeded')
        if not force and now-self.last_waiter_check<.01:
            return
        self.last_waiter_check=now
        self.checking = True
        try:
            # Administrative observation only; never kill or alter a requesting transaction.
            with self.c.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM information_schema.INNODB_LOCK_WAITS w "
                    "JOIN information_schema.INNODB_TRX b ON b.trx_id=w.blocking_trx_id "
                    "WHERE b.trx_mysql_thread_id=CONNECTION_ID()")
                if cur.fetchone()['n']:
                    raise Deferred('Correction is blocking another transaction')
        finally:
            self.checking = False

    def q(self, sql, params=()):
        self.check_priority()
        result = super().q(sql, params)
        self.check_priority()
        return result

    def rows(self, table, key, ids, lock=False):
        if not ids:
            return []
        if not lock:
            return super().rows(table,key,ids,lock=False)
        if (table,key) not in {('radacct','radacctid'),('radpostauth','id'),('wisp_vouchers','id')}:
            raise Refused('Unsupported table')
        if len(ids)!=len(set(ids)) or any(type(i) is not int or i<1 for i in ids):
            raise Refused('Invalid frozen identifiers')
        rows=self.q(f'SELECT * FROM {table} WHERE {key} IN ('+','.join(['%s']*len(ids))+
                    f') ORDER BY {key} FOR UPDATE NOWAIT',sorted(ids))
        if len(rows)!=len(ids):
            raise Refused('Missing frozen row')
        return rows

    def execute(self, plan, *, rollback=False, checkpoint=None):
        self.started=time.monotonic()
        self.last_waiter_check=0
        self.running=True
        def hook(stage):
            if checkpoint:
                checkpoint(stage)
            self.check_priority(force=True)
            if stage=='locked':
                # Recheck after hooks/races, before any historical UPDATE. RC takes no missing-key gap lock.
                self.guards([r['radacctid'] for r in plan['acct']])
        try:
            return super().execute(plan,rollback=rollback,checkpoint=hook)
        except Exception as exc:
            # MariaDB NOWAIT/lock wait, deadlock, statement deadline. No retries, no writer is killed.
            if getattr(exc,'args',()) and exc.args[0] in (1205,1213,1969,3572):
                raise Deferred('Busy or timed-out correction was rolled back') from exc
            raise
        finally:
            self.running=False
            self.timings.append(round((time.monotonic()-self.started)*1000,3))

    def split_plan(self, plan):
        expected=dict(plan);sha=expected.pop('sha256')
        if digest(expected)!=sha:
            raise Refused('Manifest changed')
        known={c['username'] for c in plan['cards']}
        leftover=[r for r in plan['auth'] if r['username'] not in known]
        children=[]
        for i,card in enumerate(sorted(plan['cards'],key=lambda c:c['id'])):
            child=copy.deepcopy(plan)
            child.pop('sha256')
            child['parent_manifest']=sha
            child['cards']=[copy.deepcopy(card)]
            child['acct']=[copy.deepcopy(r) for r in plan['acct'] if r['username']==card['username']]
            child['auth']=[copy.deepcopy(r) for r in plan['auth'] if r['username']==card['username']]
            if i==0:
                child['auth'].extend(copy.deepcopy(leftover))
                child['auth'].sort(key=lambda r:r['id'])
            if not child['acct']:
                raise Refused('Card has no frozen historical accounting')
            child['sha256']=digest(child)
            children.append(child)
        if sum(len(c['acct']) for c in children)!=len(plan['acct']) or sum(len(c['auth']) for c in children)!=len(plan['auth']):
            raise Refused('Frozen scope was not partitioned exactly')
        return children

    def run_scoped(self, plan, *, rollback=False, checkpoint=None):
        """One card per transaction; stop at first conflict, preserving completed receipts.

        No automatic retries or automatic rollback over newer traffic. Partial
        completion is explicit and safely resumable from each child's journal.
        """
        completed=[]
        for child in self.split_plan(plan):
            try:
                status=self.execute(child,rollback=rollback,checkpoint=checkpoint)
            except Refused as exc:
                return {'complete':False,'completed':completed,'deferred_manifest':child['sha256'],
                        'reason':str(exc),'automatic_retry':False}
            completed.append({'manifest':child['sha256'],'status':status})
            # No sleep while holding locks. Yield CPU/IO between independent committed cards.
            if self.pause:
                time.sleep(self.pause)
        return {'complete':True,'completed':completed,'automatic_retry':False}
