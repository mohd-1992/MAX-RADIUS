"""Snapshot retention and safe publication, without application imports or DB IO."""
import datetime as dt
import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from services import backup_compaction as b


NOW = dt.datetime(2026, 10, 31, 12)
COLS = ('radacctid', 'acctuniqueid', 'acctsessionid', 'username', 'account_username',
        'account_read_username', 'account_subscriber_id', 'account_voucher_id',
        'acctstarttime', 'acctstoptime', 'acctterminatecause')


def literal(x):
    if x is None:
        return b'NULL'
    if isinstance(x, int):
        return str(x).encode()
    x = str(x).encode()
    return b"'" + x.replace(b'\\', b'\\\\').replace(b"'", b"\\'") + b"'"


def table(name, columns, rows=(), timestamp=False, spaced=False):
    ddl = b'CREATE TABLE `' + name.encode() + b'` (\n'
    ddl += b',\n'.join(b'  `' + c.encode() + b'` ' + (b'timestamp' if timestamp and c == 'authdate' else b'text') for c in columns)
    ddl += b'\n);\n'
    if rows:
        join = b', ' if spaced else b','
        ddl += b'INSERT INTO `' + name.encode() + b'` VALUES\n'
        ddl += b',\n'.join(b'(' + join.join(map(literal, row)) + b')' for row in rows) + b';\n'
    return ddl


def session(i, **kw):
    values = dict(zip(COLS, (i, 'uid'+str(i), 'sess'+str(i), 'orphan', None, 'orphan',
                            None, None, '2026-06-01', '2026-07-01', 'User-Request')))
    values.update(kw)
    return tuple(values[c] for c in COLS)


def dump(rows, owners=(), baselines=(), auth=(), timestamp=False, zone=True, spaced=False):
    # Accounts deliberately follow accounting data: filter must not use live DB state.
    raw = b"/*!40103 SET TIME_ZONE='+00:00' */;\n" if zone else b''
    raw += table('radacct', COLS, rows, spaced=spaced)
    raw += table('wisp_subscribers', ('id','username','last_renewed_at'))
    raw += table('wisp_vouchers', ('id','username','last_renewed_at'), owners, spaced=spaced)
    raw += table('wisp_session_baselines', ('radacctid','renewed_at'), baselines)
    raw += table('radpostauth', ('id','username','authdate'), auth, timestamp=timestamp)
    raw += table('financial_test', ('id','amount'), ((1,12345),))
    return raw


def run_compact(raw, **kw):
    stream = io.BytesIO(raw)
    receipt = b.compact_dump(stream, NOW, **kw)
    retained = {}
    for name, columns, statement in b._events(stream):
        if name in b.TARGETS:
            retained.setdefault(name, []).extend(row for _, row in b._rows(statement, columns))
    return receipt, stream.getvalue(), retained


class RetentionTests(unittest.TestCase):
    def test_ownership_rename_baseline_and_uncertain_closure(self):
        rows = [session(1), session(2,account_voucher_id=1),
                session(3,account_voucher_id=2,username='old-name'),
                session(4), session(5,acctstoptime=None),
                session(6,acctterminatecause='Watchdog-Autoheal-Timeout'),
                session(7,acctterminatecause='Backup-Restored-Closed'),
                session(8,acctterminatecause='Stale-Session-Timeout'),
                session(9,acctstoptime='2026-08-02 12:00:00'),
                session(10,account_voucher_id=3),
                session(11,account_voucher_id=1,account_subscriber_id=1)]
        receipt, _, kept = run_compact(dump(rows, ((1,'current',None),(2,'renamed','2026-01-01'),(3,'renewed','2026-09-01')), ((4,'2026-09-01'),)))
        self.assertEqual(receipt['omitted']['radacct'], 2)
        self.assertEqual({int(r['radacctid']) for r in kept['radacct']}, {2,3,4,5,6,7,8,9,11})

    def test_exact_name_with_escaped_quote_and_comma(self):
        name = "a,b'\\c"
        _, _, rows = run_compact(dump([session(1,username=name,account_read_username=name)], ((1,name,None),), spaced=True))
        self.assertEqual(len(rows['radacct']), 1)

    def test_unknown_identity_and_invalid_end_keep(self):
        receipt, _, rows = run_compact(dump([session(1,acctuniqueid='',acctsessionid='',acctstarttime=None),session(2,acctstoptime='0000-00-00 00:00:00')]))
        self.assertFalse(receipt['omitted'])
        self.assertEqual(len(rows['radacct']), 2)

    def test_postauth_datetime_boundary_and_open_account_protection(self):
        auth = ((1,'other','2026-09-01'),(2,'open','2026-09-01'),(3,'other','2026-10-01 12:00:00'))
        receipt, _, rows = run_compact(dump([session(1,username='open',acctstoptime=None)], auth=auth))
        self.assertEqual(receipt['omitted']['radpostauth'], 1)
        self.assertEqual({int(r['id']) for r in rows['radpostauth']}, {2,3})

    def test_timestamp_dump_utc_is_distinct_from_storage_datetime(self):
        auth = ((1,'other','2026-10-01 08:59:59'),(2,'other','2026-10-01 09:00:00'))
        receipt, _, rows = run_compact(dump([], auth=auth, timestamp=True))
        self.assertEqual(receipt['auth_clock'], 'dump_timestamp')
        self.assertEqual({int(r['id']) for r in rows['radpostauth']}, {2})

    def test_unknown_timestamp_timezone_does_not_prune(self):
        receipt, _, rows = run_compact(dump([], auth=((1,'other','2025-01-01'),), timestamp=True, zone=False))
        self.assertEqual(receipt['auth_clock'], 'unknown_timestamp')
        self.assertEqual(len(rows['radpostauth']), 1)

    def test_non_target_sql_exact_and_source_not_modified_on_failure(self):
        raw = dump([session(1)])
        _, output, _ = run_compact(raw)
        self.assertIn(table('financial_test', ('id','amount'), ((1,12345),)), output)
        stream = io.BytesIO(raw.replace(b'CREATE TABLE `wisp_vouchers`', b'CREATE TABLE `missing_owners`'))
        with self.assertRaises(ValueError):
            b.compact_dump(stream, NOW)
        self.assertEqual(stream.getvalue(), raw.replace(b'CREATE TABLE `wisp_vouchers`', b'CREATE TABLE `missing_owners`'))

    def test_truncated_insert_and_large_statement_fail_closed(self):
        for raw in (dump([session(1)]).replace(b"'User-Request');", b"'User-Request')"), b'x'*(b.MAX_STATEMENT+1)):
            stream = io.BytesIO(raw)
            with self.assertRaises(ValueError):
                b.compact_dump(stream, NOW)
            self.assertEqual(stream.getvalue(), raw)

    def test_no_shorter_retention(self):
        with self.assertRaises(ValueError):
            run_compact(dump([]), session_days=30)

    def test_history_sort_is_disk_backed_and_keeps_rows(self):
        raw = dump([]) + table('wisp_imported_session_history', COLS,
                              (session(1,username='z',acctstoptime='2026-10-20'),session(2,username='a',acctstoptime='2026-10-20')))
        _, _, rows = run_compact(raw)
        self.assertEqual([r['username'] for r in rows['wisp_imported_session_history']], [b'a',b'z'])


class NativeReaderTests(unittest.TestCase):
    def test_projected_decode_keeps_original_full_row_literal(self):
        raw=b"INSERT INTO `t` VALUES (7,'full payload',NULL);\n"
        literal_value,decoded=next(b._rows(raw,('id','payload','nullable'),('id','nullable')))
        self.assertEqual(literal_value,b"(7,'full payload',NULL)")
        self.assertEqual(decoded,{'id':b'7','nullable':None})

    def test_projection_still_validates_the_full_column_contract(self):
        with self.assertRaises(ValueError):
            list(b._rows(b"INSERT INTO `t` VALUES (7,'extra');\n",('id',),('id',)))

    def test_multiline_insert_preserves_sql_bytes_and_column_contract(self):
        prefix = b'INSERT INTO `radpostauth` (`id`,`username`,`authdate`) VALUES\n'
        raw = prefix + b"(1,'semi;colon','2026-10-31'),\n\n(2,'quote\\\'name','2026-10-31')\n;\n"
        following = b'SET FOREIGN_KEY_CHECKS=1;\n'
        events = list(b._events(io.BytesIO(raw + following)))
        self.assertEqual(events, [('radpostauth', ('id','username','authdate'), raw),
                                  (None, None, following)])

    def test_multiline_statement_budget_is_cumulative_and_exact(self):
        raw = b'INSERT INTO `radpostauth` (`id`) VALUES\n' + b'(1),\n' * 10 + b'(2);\n'
        with patch.object(b, 'MAX_STATEMENT', len(raw)):
            self.assertEqual(list(b._events(io.BytesIO(raw)))[0][2], raw)
        with patch.object(b, 'MAX_STATEMENT', len(raw)-1):
            with self.assertRaises(ValueError):
                list(b._events(io.BytesIO(raw)))

    def test_truncated_multiline_statement_is_rejected(self):
        raw = b'INSERT INTO `radpostauth` (`id`) VALUES\n(1),\n\n'
        with self.assertRaises(ValueError):
            list(b._events(io.BytesIO(raw)))


class PublicationTests(unittest.TestCase):
    def test_same_storage_different_destinations_are_serialized(self):
        with tempfile.TemporaryDirectory() as work:
            a=Path(work)/'one';a.mkdir()
            c=Path(work)/'two';c.mkdir()
            with b.backup_lock(a, work_directory=work):
                with self.assertRaises(ValueError):
                    with b.backup_lock(c, work_directory=work):
                        pass

    def test_lock_contention_and_marked_orphan_cleanup_only(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work)
            orphan=root/'max-backup-sort-abcdefgh';orphan.mkdir()
            (orphan/'.max-backup-owner').write_bytes(b'max-radius-backup-work-v1\n')
            (orphan/'sort.sqlite').write_bytes(b'dead process work')
            foreign=root/'max-backup-sort-ijklmnop';foreign.mkdir()
            (foreign/'keep').write_bytes(b'not ours')
            partial=root/'.max-backup-abcdefgh.partial';partial.write_bytes(b'incomplete')
            complete=root/'keep.tar.gz';complete.write_bytes(b'published')
            with b.backup_lock(work):
                with self.assertRaises(ValueError):
                    with b.backup_lock(work):
                        pass
            self.assertFalse(orphan.exists())
            self.assertFalse(partial.exists())
            self.assertTrue(foreign.exists())
            self.assertEqual(complete.read_bytes(), b'published')

    def test_standard_gzip_and_atomic_publish(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work)/'backup.tar.gz'
            with b.atomic_tarfile(path) as tar:
                self.assertFalse(path.exists())
                info = tarfile.TarInfo('database_dump.sql');info.size=8
                tar.addfile(info, io.BytesIO(b'SELECT 1'))
            with gzip.open(path, 'rb') as f:
                self.assertIn(b'SELECT 1', f.read())
            self.assertFalse(list(Path(work).glob('*.partial')))

    def test_write_error_and_destination_collision(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work)/'backup.tar.gz'
            with self.assertRaises(OSError):
                with b.atomic_tarfile(path):
                    raise OSError('simulated disk full')
            self.assertFalse(path.exists())
            self.assertFalse(list(Path(work).glob('*.partial')))
            path.write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                with b.atomic_tarfile(path):
                    pass
            self.assertEqual(path.read_bytes(), b'original')

    def test_private_work_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as work:
            stream=io.BytesIO(b'INSERT INTO `radacct` VALUES broken;\n')
            with self.assertRaises(ValueError):
                b.compact_dump(stream, NOW, work_parent=work)
            self.assertEqual(list(Path(work).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
