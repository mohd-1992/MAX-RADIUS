"""Backup-only retention over a consistent SQL snapshot, using private disk work.

Never deletes live accounting. The old gzip/tar reader remains compatible.
"""
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
import zlib

DETAIL_TABLES = ('radacct', 'radacct_archive', 'wisp_imported_session_history', 'wisp_traffic_history')
HISTORY_TABLES = DETAIL_TABLES[2:]
TARGETS = DETAIL_TABLES + ('radpostauth',)
META_TABLES = ('wisp_subscribers', 'wisp_vouchers', 'wisp_session_baselines')
MAX_STATEMENT = 8 * 1024 * 1024
MAX_ROW = 1024 * 1024
POLICY_COLUMNS = frozenset(('id','username','last_renewed_at','radacctid','acctuniqueid',
    'acctsessionid','acctstarttime','acctstoptime','acctterminatecause','authdate',
    'account_username','account_read_username','account_subscriber_id','account_voucher_id'))
UNCERTAIN = {b'stale-session-timeout', b'watchdog-autoheal-timeout', b'backup-restored-closed'}
INSERT = re.compile(rb'^INSERT INTO `([A-Za-z0-9_]+)`(?:\s*\(([^;]*?)\))? VALUES\s*', re.S)
CREATE = re.compile(rb'^CREATE TABLE(?: IF NOT EXISTS)? `([A-Za-z0-9_]+)`')
COLUMN = re.compile(rb'^\s+`([A-Za-z0-9_]+)`\s+')
ROW = re.compile(rb"\((?:'(?:\\.|[^'\\])*'|[^'()])*\)")
VALUE = re.compile(rb"\s*(?:'(?:\\.|[^'\\])*'|[^,'\s][^,]*)")


def _literal(value):
    if value == b'NULL':
        return None
    if value.startswith(b"'") and value.endswith(b"'"):
        if b'\\' not in value:
            return value[1:-1]
        escapes = {48: b'\0', 110: b'\n', 114: b'\r', 116: b'\t', 98: b'\b', 90: b'\x1a'}
        return re.sub(rb'\\(.)', lambda m: escapes.get(m[1][0], m[1]), value[1:-1])
    if value.startswith(b'0x'):
        return bytes.fromhex(value[2:].decode('ascii'))
    return value


def _date(value):
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(value.decode('ascii'))
    except (ValueError, UnicodeError):
        return None


def _events(stream, targets=None):
    """Bounded native/legacy dump statements; unknown input fails before publish."""
    stream.seek(0)
    columns = {}
    schema = None
    while True:
        line = stream.readline(MAX_STATEMENT + 1)
        if not line:
            break
        if len(line) > MAX_STATEMENT:
            raise ValueError('SQL statement exceeds the bounded backup budget.')
        create = CREATE.match(line)
        if create:
            schema = create[1].decode('ascii')
            columns[schema] = []
        if schema:
            column = COLUMN.match(line)
            if column:
                columns[schema].append(column[1].decode('ascii'))
            if line.startswith(b')'):
                schema = None
        match = INSERT.match(line)
        if not match or match[1].decode('ascii') not in (targets if targets is not None else TARGETS + META_TABLES):
            yield None, None, line
            continue
        raw = line
        if not line.rstrip().endswith(b';'):
            # Native dumps can split a single INSERT over thousands of lines.
            # Grow one bounded buffer instead of copying every previous line
            # again for each append. Check the last line, not the whole buffer.
            buffer = bytearray(line)
            last = line
            while not last.rstrip().endswith(b';'):
                last = stream.readline(MAX_STATEMENT + 1)
                if not last or len(buffer) + len(last) > MAX_STATEMENT:
                    raise ValueError('Incomplete or oversized SQL insert in backup.')
                buffer.extend(last)
            raw = bytes(buffer)
        match = INSERT.match(raw)
        table = match[1].decode('ascii')
        fields = ([x.decode('ascii') for x in re.findall(rb'`([A-Za-z0-9_]+)`', match[2])]
                  if match[2] else columns.get(table))
        if not fields:
            raise ValueError('Missing backup column contract for ' + table)
        yield table, tuple(fields), raw


def _rows(raw, columns, projection=None):
    start = INSERT.match(raw).end()
    cursor = start
    found = False
    selected = None if projection is None else tuple((i,k) for i,k in enumerate(columns) if k in projection)
    for match in ROW.finditer(raw, start):
        if raw[cursor:match.start()].strip(b' \t\r\n,'):
            raise ValueError('Unrecognized SQL value syntax.')
        row = match[0]
        if len(row) > MAX_ROW:
            raise ValueError('SQL row exceeds the bounded backup budget.')
        values = VALUE.findall(row[1:-1])
        if len(values) != len(columns):
            raise ValueError('SQL backup row does not match its column contract.')
        decoded = (dict(zip(columns, (_literal(v.strip()) for v in values))) if selected is None else
                   {k:_literal(values[i].strip()) for i,k in selected})
        yield row, decoded
        cursor = match.end()
        found = True
    if not found or raw[cursor:].strip() != b';':
        raise ValueError('Unrecognized or incomplete SQL insert.')


def _integer(value):
    return None if value is None else int(value)


def _eligible(db, row, cutoff):
    """Conservative snapshot equivalent of HISTORY_ELIGIBLE plus all baselines."""
    end = _date(row.get('acctstoptime'))
    if end is None or end >= cutoff:
        return False
    if (row.get('acctterminatecause') or b'').lower() in UNCERTAIN:
        return False
    if not (row.get('acctuniqueid') or row.get('acctsessionid') or row.get('acctstarttime')):
        return False
    sid, vid = _integer(row.get('account_subscriber_id')), _integer(row.get('account_voucher_id'))
    if sid is not None and vid is not None:
        return False
    rid = _integer(row.get('radacctid'))
    if db.execute('SELECT 1 FROM baseline WHERE rid=? LIMIT 1', (rid,)).fetchone():
        return False
    name = row.get('account_read_username')
    if name is None:
        name = row.get('account_username') or row.get('username') or b''
    start = _date(row.get('acctstarttime'))
    for kind, own, other in (('subscriber', sid, vid), ('voucher', vid, sid)):
        if other is not None:
            continue
        if own is not None:
            accounts = db.execute('SELECT renewed FROM owner WHERE kind=? AND oid=?', (kind, own)).fetchall()
        else:
            accounts = db.execute('SELECT renewed FROM owner WHERE kind=? AND name=? LIMIT 2', (kind, name)).fetchall()
            if len(accounts) > 1:
                return False
        for (renewed,) in accounts:
            boundary = _date(renewed)
            if boundary is None or start is None or start >= boundary:
                return False
    return True


def compact_dump(stream, snapshot_at, work_parent=None, session_days=90, auth_days=30):
    """Replace only a private dump file; never queries or changes a live DB.

    SQLite contains disk-backed sort rows and snapshot ownership, cache <=4MiB.
    Every temporary artifact is removed on success/failure by its own context.
    """
    if session_days < 90 or auth_days < 30:
        raise ValueError('Backup retention cannot be shorter than the accepted policy.')
    storage_timezone = dt.timezone(dt.timedelta(hours=3))
    reference = snapshot_at if snapshot_at.tzinfo is not None else snapshot_at.replace(tzinfo=storage_timezone)
    snapshot_at = reference.astimezone(storage_timezone).replace(tzinfo=None)
    receipt = dict(format=1, scope='backup_only', session_days=session_days, auth_days=auth_days,
                   snapshot_at=snapshot_at.isoformat(' '), omitted={}, retained={},
                   financial_records='complete', session_sort='disk_username_start')
    cutoff = snapshot_at - dt.timedelta(days=session_days)
    auth_cutoff = snapshot_at - dt.timedelta(days=auth_days)
    with tempfile.TemporaryDirectory(prefix='max-backup-sort-', dir=work_parent) as work:
        (Path(work) / '.max-backup-owner').write_bytes(b'max-radius-backup-work-v1\n')
        db = sqlite3.connect(str(Path(work) / 'sort.sqlite'))
        try:
            db.executescript('''PRAGMA cache_size=-4096; PRAGMA temp_store=FILE;
                PRAGMA mmap_size=0; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                CREATE TABLE owner(kind TEXT,oid INTEGER,name BLOB,renewed BLOB,PRIMARY KEY(kind,oid));
                CREATE INDEX owner_name ON owner(kind,name);
                CREATE TABLE baseline(rid INTEGER PRIMARY KEY);
                CREATE TABLE auth_protect(name BLOB PRIMARY KEY);
                CREATE TABLE kept(t TEXT,n INTEGER,u BLOB,s BLOB,r BLOB,PRIMARY KEY(t,n));
                CREATE INDEX sort_history ON kept(t,u,s,n);''')
            # Pass1: owner and baseline state comes from the very same native snapshot.
            schemas = set()
            auth_is_timestamp = False
            dump_timezone = None
            for table, columns, raw in _events(stream):
                timezone = re.search(rb"\bSET TIME_ZONE\s*=\s*'([+-]\d\d:\d\d)'", raw)
                if timezone:
                    zone = timezone[1].decode('ascii')
                    minutes = (int(zone[1:3]) * 60 + int(zone[4:6])) * (-1 if zone[0] == '-' else 1)
                    dump_timezone = dt.timezone(dt.timedelta(minutes=minutes))
                if re.match(rb'^\s+`authdate`\s+timestamp\b', raw, re.I):
                    auth_is_timestamp = True
                definition = CREATE.match(raw)
                if definition:
                    schemas.add(definition[1].decode('ascii'))
                if table not in META_TABLES + DETAIL_TABLES:
                    continue
                for _, row in _rows(raw, columns, POLICY_COLUMNS):
                    if table in META_TABLES[:2]:
                        kind = 'subscriber' if table == 'wisp_subscribers' else 'voucher'
                        db.execute('INSERT INTO owner VALUES(?,?,?,?)', (kind, _integer(row['id']), row['username'], row.get('last_renewed_at')))
                    elif table == 'wisp_session_baselines':
                        db.execute('INSERT OR IGNORE INTO baseline VALUES(?)', (_integer(row['radacctid']),))
                    elif row.get('acctstoptime') is None or (row.get('acctterminatecause') or b'').lower() in UNCERTAIN:
                        for field in ('username', 'account_username', 'account_read_username'):
                            if row.get(field):
                                db.execute('INSERT OR IGNORE INTO auth_protect VALUES(?)', (row[field],))
            if not set(META_TABLES).issubset(schemas):
                raise ValueError('Backup snapshot is missing ownership or baseline tables.')
            if auth_is_timestamp:
                auth_cutoff = (reference.astimezone(dump_timezone).replace(tzinfo=None) - dt.timedelta(days=auth_days)
                               if dump_timezone is not None else None)
            receipt['auth_clock'] = ('dump_timestamp' if dump_timezone else 'unknown_timestamp') if auth_is_timestamp else 'storage_datetime'
            db.commit()
            contracts = {}
            serial = 0
            # Pass2: bounded parse/filter, all retained target rows spill to disk.
            for table, columns, raw in _events(stream):
                if table not in TARGETS:
                    continue
                if table in contracts and contracts[table] != columns:
                    raise ValueError('Changing insert column order is unsupported for ' + table)
                contracts[table] = columns
                for literal, row in _rows(raw, columns, POLICY_COLUMNS):
                    if table == 'radpostauth':
                        date = _date(row.get('authdate'))
                        omit = bool(date and auth_cutoff is not None and date < auth_cutoff and not db.execute(
                            'SELECT 1 FROM auth_protect WHERE name=?', (row.get('username'),)).fetchone())
                    else:
                        omit = _eligible(db, row, cutoff)
                    counter = receipt['omitted'] if omit else receipt['retained']
                    counter[table] = counter.get(table, 0) + 1
                    if not omit:
                        serial += 1
                        db.execute('INSERT INTO kept VALUES(?,?,?,?,?)',
                                   (table, serial, row.get('username') or b'', row.get('acctstarttime') or b'', literal))
                        if serial % 1000 == 0:
                            db.commit()
            db.commit()
            # Pass3: copy all other SQL exactly, replace target inserts once per table.
            with tempfile.TemporaryFile(mode='w+b', dir=work) as output:
                written = set()
                for table, columns, raw in _events(stream):
                    if table not in TARGETS:
                        output.write(raw)
                        continue
                    if table in written:
                        continue
                    written.add(table)
                    order = 'u,s,n' if table in HISTORY_TABLES else 'n'
                    cursor = db.execute('SELECT r FROM kept WHERE t=? ORDER BY ' + order, (table,))
                    prefix = ('INSERT INTO `' + table + '` (' + ','.join('`'+c+'`' for c in columns) + ') VALUES\n').encode()
                    batch, size = [], 0
                    for (literal,) in cursor:
                        if batch and (len(batch) >= 256 or size + len(literal) > 256 * 1024):
                            output.write(prefix + b',\n'.join(batch) + b';\n')
                            batch, size = [], 0
                        batch.append(literal)
                        size += len(literal)
                    if batch:
                        output.write(prefix + b',\n'.join(batch) + b';\n')
                output.flush()
                receipt['sql_bytes'] = output.tell()
                output.seek(0)
                stream.seek(0)
                stream.truncate()
                shutil.copyfileobj(output, stream, 1024 * 1024)
                stream.flush()
                stream.seek(0)
        finally:
            db.close()
    return receipt


class _FilteredGzip:
    def __init__(self, target):
        self.target = target
        self.compressor = zlib.compressobj(6, zlib.DEFLATED, 31, 8, zlib.Z_FILTERED)

    def write(self, data):
        self.target.write(self.compressor.compress(data))
        return len(data)

    def finish(self):
        self.target.write(self.compressor.flush())


@contextmanager
def backup_lock(directory, work_directory=None):
    """Backup-specific cross-process lock, independent of the 2.7 heavy job system."""
    # Storage is shared by every worker even if the configured export destination
    # changes. Never let two destination locks clean each other's active sort.
    with open(Path(work_directory or directory) / '.max-backup.lock', 'a+b') as lock:
        try:
            import fcntl
        except ImportError:
            fcntl = None
            import msvcrt
            lock.seek(0, 2)
            if not lock.tell():
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
        try:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            raise ValueError('توجد عملية إنشاء نسخة احتياطية قيد التنفيذ.') from None
        try:
            # A dead creator cannot retain this lock. Clear only our marked work,
            # never a published archive or an arbitrary directory in storage.
            roots = {Path(directory).resolve(), Path(work_directory or directory).resolve()}
            for root in roots:
                for old in root.iterdir():
                    if old.is_symlink() or old.resolve().parent != root:
                        continue
                    if re.fullmatch(r'\.max-backup-[a-z0-9_]{8}\.partial', old.name) and old.is_file():
                        old.unlink()
                    elif re.fullmatch(r'max-backup-sort-[a-z0-9_]{8}', old.name) and old.is_dir():
                        marker = old / '.max-backup-owner'
                        if marker.is_file() and not marker.is_symlink() and marker.read_bytes() == b'max-radius-backup-work-v1\n':
                            shutil.rmtree(old)
            yield
        finally:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def atomic_tarfile(filepath):
    """Only a complete fsynced archive becomes discoverable as .tar.gz."""
    filepath = Path(filepath)
    descriptor, partial = tempfile.mkstemp(prefix='.max-backup-', suffix='.partial', dir=filepath.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            compressed = _FilteredGzip(target)
            with tarfile.open(fileobj=compressed, mode='w|', dereference=True) as archive:
                yield archive
            compressed.finish()
            target.flush()
            os.fsync(target.fileno())
        if filepath.exists():
            raise FileExistsError('Backup destination already exists.')
        os.replace(partial, filepath)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
