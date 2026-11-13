"""Explicit MariaDB fixture: DB_NAME=reservation_index_fixture, --run only.

Tests online DDL and the real installed Stop trigger, including the unindexed
lock amplification. Never run through unrestricted unittest discovery.
"""
import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
import unittest

AUTHORIZED = (os.environ.get('DB_HOST') == 'max-license-test-db'
              and os.environ.get('DB_NAME') == 'reservation_index_fixture'
              and sys.argv[1:] == ['--run'])
if not AUTHORIZED:
    raise unittest.SkipTest('Explicit isolated reservation_index_fixture and --run required')

import pymysql
from database.reservation_indexes import ensure_reservation_release_index, _state


def connect(autocommit=True, dictionary=True):
    return pymysql.connect(host='max-license-test-db', user='radius', password='radpass',
                           database='reservation_index_fixture', autocommit=autocommit,
                           cursorclass=pymysql.cursors.DictCursor if dictionary else pymysql.cursors.Cursor)


class ReleaseIndex(unittest.TestCase):
    def setUp(self):
        self.c = connect()
        self.q = self.c.cursor()
        self.q.execute('DELETE FROM radacct')
        self.q.execute('DROP TABLE IF EXISTS wisp_session_reservations')
        self.q.execute("""CREATE TABLE wisp_session_reservations (
            id INT AUTO_INCREMENT PRIMARY KEY, radacctid BIGINT NULL,
            session_key VARCHAR(191) NOT NULL, username VARCHAR(64) NOT NULL,
            nasipaddress VARCHAR(45) NOT NULL, callingstationid VARCHAR(50) NOT NULL,
            nasportid VARCHAR(50) NOT NULL DEFAULT '', reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL, UNIQUE KEY uk_session_key(session_key),
            KEY ix_userman_reservation_user(username), KEY idx_res_expires(expires_at),
            KEY idx_res_nas(nasipaddress)) ENGINE=InnoDB""")

    def tearDown(self):
        self.q.close()
        self.c.close()

    def seed(self, count=25):
        self.q.executemany("""INSERT INTO wisp_session_reservations
            (radacctid,session_key,username,nasipaddress,callingstationid,expires_at)
            VALUES(%s,%s,%s,'10.1.0.1','AA',NOW()+INTERVAL 1 HOUR)""",
            [(n if n % 2 else None, str(n), 'reservation-fixture-'+str(n)) for n in range(1,count+1)])

    def digest(self):
        self.q.execute('SELECT * FROM wisp_session_reservations ORDER BY id')
        return hashlib.sha256(json.dumps(self.q.fetchall(),default=str,sort_keys=True).encode()).hexdigest()

    def test_add_repeat_preserves_all_rows_and_bounded_lease(self):
        self.seed()
        self.q.execute('SELECT * FROM wisp_license_runtime_state ORDER BY id')
        lease=self.q.fetchall()
        before=self.digest()
        self.assertTrue(ensure_reservation_release_index(self.c))
        self.assertFalse(ensure_reservation_release_index(self.c))
        self.assertEqual(before,self.digest())
        self.q.execute('SELECT * FROM wisp_license_runtime_state ORDER BY id')
        self.assertEqual(lease,self.q.fetchall())

    def test_legacy_missing_column_is_added_without_data_change(self):
        self.q.execute('ALTER TABLE wisp_session_reservations DROP COLUMN radacctid')
        self.seed_legacy = """INSERT INTO wisp_session_reservations
            (session_key,username,nasipaddress,callingstationid,expires_at)
            VALUES('legacy','legacy','10.1.0.1','AA',NOW())"""
        self.q.execute(self.seed_legacy)
        self.assertTrue(ensure_reservation_release_index(self.c))
        self.q.execute("SELECT username,radacctid FROM wisp_session_reservations")
        self.assertEqual(self.q.fetchone(),dict(username='legacy',radacctid=None))

    def test_equivalent_composite_leading_index_is_reused(self):
        self.q.execute('ALTER TABLE wisp_session_reservations ADD INDEX custom_release(radacctid,username)')
        self.assertFalse(ensure_reservation_release_index(self.c))
        self.q.execute('SHOW INDEX FROM wisp_session_reservations')
        self.assertNotIn('idx_res_radacctid',{r['Key_name'] for r in self.q.fetchall()})

    def test_nonleading_index_does_not_mask_defect(self):
        self.q.execute('ALTER TABLE wisp_session_reservations ADD INDEX wrong_order(username,radacctid)')
        self.assertTrue(ensure_reservation_release_index(self.c))

    def test_ignored_index_does_not_mask_defect(self):
        self.q.execute('ALTER TABLE wisp_session_reservations ADD INDEX ignored_release(radacctid) IGNORED')
        self.assertTrue(ensure_reservation_release_index(self.c))

    def test_conflicting_name_fails_without_replacing_user_index(self):
        self.q.execute('ALTER TABLE wisp_session_reservations ADD INDEX idx_res_radacctid(username)')
        with self.assertRaisesRegex(RuntimeError,'not a usable'):
            ensure_reservation_release_index(self.c)
        self.q.execute('SHOW INDEX FROM wisp_session_reservations WHERE Key_name="idx_res_radacctid"')
        self.assertEqual(self.q.fetchone()['Column_name'],'username')

    def test_active_transaction_is_not_implicitly_committed(self):
        c=connect(False)
        try:
            with c.cursor() as q:
                q.execute("INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,expires_at) VALUES('pending','pending','x','x',NOW())")
            with self.assertRaisesRegex(RuntimeError,'committed schema boundary'):
                ensure_reservation_release_index(c)
            c.rollback()
        finally:c.close()
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations')
        self.assertEqual(self.q.fetchone()['n'],0)

    def test_tuple_cursor_supported(self):
        c=connect(dictionary=False)
        try:self.assertTrue(ensure_reservation_release_index(c))
        finally:c.close()

    def test_noop_does_not_commit_existing_transaction(self):
        ensure_reservation_release_index(self.c)
        c=connect(False)
        try:
            with c.cursor() as q:
                q.execute("INSERT INTO wisp_session_reservations(session_key,username,nasipaddress,callingstationid,expires_at) VALUES('pending','pending','x','x',NOW())")
            self.assertFalse(ensure_reservation_release_index(c))
            c.rollback()
        finally:c.close()
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations')
        self.assertEqual(self.q.fetchone()['n'],0)

    def test_sql_migration_index_block_is_repeatable_and_data_preserving(self):
        from pathlib import Path
        self.seed()
        before=self.digest()
        source=(Path(__file__).resolve().parents[1]/'database/migrations/20261005_active_sessions_licensing.sql').read_text()
        block=source.split('-- Bounded online release lookup;',1)[1].split('\n\n',1)[0]
        block=block[block.index('SET @reservation_previous'):]
        for _ in range(2):
            for statement in block.split(';'):
                if statement.strip():self.q.execute(statement)
        self.assertTrue(_state(self.q)[2])
        self.assertEqual(before,self.digest())

    def test_concurrent_initializers(self):
        barrier=threading.Barrier(6)
        def install(_):
            c=connect()
            try:
                barrier.wait(timeout=10)
                return ensure_reservation_release_index(c)
            finally:c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(install,range(6)))
        self.assertTrue(any(results))
        self.assertFalse(ensure_reservation_release_index(self.c))
        self.q.execute('SHOW INDEX FROM wisp_session_reservations WHERE Column_name="radacctid"')
        self.assertEqual(len(self.q.fetchall()),1)

    def test_metadata_contention_times_out_and_restores_session_then_retries(self):
        self.seed()
        self.q.execute('SET SESSION lock_wait_timeout=17')
        blocker=connect(False)
        try:
            with blocker.cursor() as b:b.execute('SELECT * FROM wisp_session_reservations LIMIT 1')
            started=time.monotonic()
            with self.assertRaises(pymysql.OperationalError) as exc:
                ensure_reservation_release_index(self.c)
            self.assertEqual(exc.exception.args[0],1205)
            self.assertLess(time.monotonic()-started,5)
            self.q.execute('SELECT @@SESSION.lock_wait_timeout AS n')
            self.assertEqual(self.q.fetchone()['n'],17)
        finally:blocker.rollback();blocker.close()
        self.assertTrue(ensure_reservation_release_index(self.c))

    def test_capacity_installer_index_only_preserves_verified_lease(self):
        from database.license_capacity import install_capacity_schema
        self.q.execute("UPDATE wisp_license_runtime_state SET is_valid=1,valid_until=UTC_TIMESTAMP()+INTERVAL 2 MINUTE WHERE id=1")
        self.q.execute('SELECT * FROM wisp_license_runtime_state')
        before=self.q.fetchall()
        install_capacity_schema(self.c)
        install_capacity_schema(self.c)
        self.q.execute('SELECT * FROM wisp_license_runtime_state')
        self.assertEqual(before,self.q.fetchall())
        self.assertTrue(_state(self.q)[2])

    def test_healer_without_trigger_install_repairs_existing_table(self):
        from database.schema_healer import heal_database_schema
        self.seed()
        before=self.digest()
        self.assertTrue(heal_database_schema(backfill=False,install_triggers=False))
        self.assertTrue(_state(self.q)[2])
        self.assertTrue(heal_database_schema(backfill=False,install_triggers=False))
        self.assertEqual(before,self.digest())

    def test_license_table_initializer_repairs_existing_table(self):
        from services.license_guard_service import ensure_license_tables
        self.seed()
        before=self.digest()
        ensure_license_tables()
        self.assertTrue(_state(self.q)[2])
        ensure_license_tables()
        self.assertEqual(before,self.digest())

    def test_expired_delete_keeps_money_summary_and_unrelated_card(self):
        from services.account_lifecycle_service import delete_account
        from unittest.mock import patch
        ensure_reservation_release_index(self.c)
        self.q.execute("INSERT INTO wisp_packages(name,service_type,price,cost,is_active) VALUES('release-index-delete-fixture','hotspot',25,7,1)")
        pkg=self.q.lastrowid
        self.q.execute("INSERT INTO wisp_voucher_batches(batch_number,name,package_id,card_count) VALUES('release-index-delete','release-index-delete',%s,2)",(pkg,))
        batch=self.q.lastrowid
        cards=[]
        for name in ('release-index-expired','release-index-untouched'):
            self.q.execute("""INSERT INTO wisp_vouchers(batch_id,package_id,serial_number,username,password,pin_code,status,last_renewed_at,expires_at)
                VALUES(%s,%s,%s,%s,'fixture','fixture','expired',NOW()-INTERVAL 60 DAY,NOW()-INTERVAL 30 DAY)""",(batch,pkg,name,name))
            cards.append(self.q.lastrowid)
        self.q.execute('SELECT * FROM wisp_vouchers WHERE id=%s',(cards[1],))
        untouched=self.q.fetchone()
        self.q.execute("""INSERT INTO wisp_voucher_sales(voucher_id,batch_id,batch_name,username,package_name,price,cost,activated_at)
            VALUES(%s,%s,'fixture','release-index-expired','fixture',25,7,NOW()-INTERVAL 40 DAY)""",(cards[0],batch))
        sale=self.q.lastrowid
        self.q.execute('SELECT * FROM wisp_voucher_sales WHERE id=%s',(sale,))
        money=self.q.fetchone()
        self.q.execute("""INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime,
            acctstoptime,acctsessiontime,acctinputoctets,acctoutputoctets,acctterminatecause)
            VALUES('release-index-expired','ab0123456789abcdef0123456789abcd','release-index-expired','10.1.0.1',
            NOW()-INTERVAL 45 DAY,NOW()-INTERVAL 44 DAY,NOW()-INTERVAL 44 DAY,86400,12345,67890,'User-Request')""")
        with patch('services.license_guard_service.get_active_license_status',return_value=dict(valid=True,status='active',features={},limits={})),patch('services.license_guard_service.has_license_feature',return_value=True):
            result=delete_account('voucher',cards[0],queue_pending=False)
        self.assertTrue(result['deleted'],result)
        self.q.execute('SELECT * FROM wisp_vouchers WHERE id=%s',(cards[1],))
        self.assertEqual(untouched,self.q.fetchone())
        self.q.execute('SELECT * FROM wisp_voucher_sales WHERE id=%s',(sale,))
        self.assertEqual(money,self.q.fetchone())
        self.q.execute('SELECT SUM(input_bytes) AS up,SUM(output_bytes) AS down,SUM(session_count) AS sessions FROM wisp_account_usage_summary WHERE voucher_id=%s',(cards[0],))
        self.assertEqual(tuple(int(v) for v in self.q.fetchone().values()),(12345,67890,1))
        self.q.execute('SELECT input_bytes,output_bytes FROM wisp_account_usage_seals WHERE voucher_id=%s',(cards[0],))
        self.assertEqual(tuple(self.q.fetchone().values()),(12345,67890))

    def test_real_stop_trigger_lock_amplification_before_and_after(self):
        self.seed(50000)
        self.q.execute("""INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,
            acctstarttime,acctupdatetime,acctinputoctets,acctoutputoctets)
            VALUES('index-stop','index-stop','index-stop','10.1.0.1',NOW(),NOW(),12345,67890)""")
        target=self.q.lastrowid
        self.q.execute('UPDATE wisp_session_reservations SET radacctid=NULL')
        self.q.execute('UPDATE wisp_session_reservations SET radacctid=%s WHERE session_key="50000"',(target,))
        plans={}
        for indexed in (False,True):
            if indexed:ensure_reservation_release_index(self.c)
            self.q.execute('EXPLAIN SELECT id FROM wisp_session_reservations WHERE radacctid=%s',(target,))
            plans[str(indexed)]=self.q.fetchone()
            blocker=connect(False);worker=connect(False)
            try:
                with blocker.cursor() as q:
                    # Match the concurrent purge statement from the incident.
                    q.execute('DELETE FROM wisp_session_reservations WHERE username="reservation-fixture-1"')
                with worker.cursor() as q:
                    q.execute('SET SESSION innodb_lock_wait_timeout=1')
                    started=time.monotonic()
                    if not indexed:
                        with self.assertRaises(pymysql.OperationalError) as exc:
                            q.execute('UPDATE radacct SET acctstoptime=NOW() WHERE radacctid=%s',(target,))
                        self.assertEqual(exc.exception.args[0],1205)
                        worker.rollback()
                    else:
                        q.execute('UPDATE radacct SET acctstoptime=NOW() WHERE radacctid=%s',(target,))
                        worker.commit()
                        self.assertLess(time.monotonic()-started,1)
            finally:
                worker.rollback();worker.close();blocker.rollback();blocker.close()
        self.assertEqual(plans['False']['type'],'ALL')
        self.assertEqual(plans['True']['key'],'idx_res_radacctid')
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations')
        self.assertEqual(self.q.fetchone()['n'],49999)
        self.q.execute('SELECT acctuniqueid,acctinputoctets,acctoutputoctets,acctstoptime FROM radacct WHERE radacctid=%s',(target,))
        row=self.q.fetchone()
        self.assertEqual((row['acctuniqueid'],row['acctinputoctets'],row['acctoutputoctets']),('index-stop',12345,67890))
        self.assertIsNotNone(row['acctstoptime'])
        print(json.dumps({'lock_comparison':plans,'unindexed':'1205 on unrelated reservation lock',
                          'indexed':'Stop committed while unrelated lock still held','rows':50000},default=str))


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseIndex))
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}))
    sys.exit(not result.wasSuccessful())
