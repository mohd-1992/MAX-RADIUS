"""Disk-bounded, backup-only aggregation of safely settled expired vouchers.

Current policy deliberately leaves cards with any archive/import/history copy
untouched. This avoids summing a logical session twice without its full identity.
No query against the live database and no modification of voucher/financial rows.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import re
import sqlite3
import tempfile
from functools import lru_cache

from services.backup_compaction import _events, _rows, _date, _integer, UNCERTAIN, DETAIL_TABLES
from database.usage_summary_schema import TABLE_SQL, SEALS_SQL

TABLE = 'wisp_account_usage_summary'
COLS = ('summary_key','voucher_id','username','reseller_id','cycle_start','first_start','last_start',
        'nasipaddress','calledstationid','nasportid','framedprotocol','servicetype','report_visible',
        'input_bytes','output_bytes','uptime_seconds','session_count')
TARGETS = DETAIL_TABLES + ('wisp_vouchers','wisp_subscribers','wisp_session_baselines',
                           'wisp_session_reservations',TABLE,'wisp_account_usage_seals',
                           'wisp_nas_session_seals','wisp_traffic_checkpoints','wisp_session_service_evidence')
SUMMARY_COLUMNS = frozenset(('id','username','status','last_renewed_at','first_used_at',
    'created_at','reseller_id','radacctid','acctuniqueid','account_subscriber_id',
    'account_voucher_id','account_read_username','account_username','acctstarttime',
    'acctstoptime','acctterminatecause','acctsessionid','acctinputoctets',
    'acctoutputoctets','acctsessiontime','expires_at','nasipaddress','calledstationid',
    'nasportid','framedprotocol','servicetype','original_username','service_type'))


def literal(value):
    if value is None:
        return b'NULL'
    if isinstance(value, int):
        return str(value).encode()
    if not isinstance(value, bytes):
        value = str(value).encode('utf8')
    return b"'"+value.replace(b'\\',b'\\\\').replace(b"'",b"\\'").replace(b'\0',b'\\0').replace(b'\n',b'\\n').replace(b'\r',b'\\r')+b"'"


def emit_rows(out, rows):
    prefix = ('INSERT INTO `'+TABLE+'` ('+','.join('`'+c+'`' for c in COLS)+') VALUES\n').encode()
    # A group may have older summary data after restoring then backing up again.
    update = b' ON DUPLICATE KEY UPDATE '+b','.join(
        ('`'+k+'`=`'+k+'`+VALUES(`'+k+'`)').encode() for k in COLS[-4:])
    update += b',first_start=LEAST(first_start,VALUES(first_start)),last_start=GREATEST(last_start,VALUES(last_start));\n'
    batch = []
    for row in rows:
        batch.append(b'('+b','.join(literal(v) for v in row)+b')')
        if len(batch) == 128:
            out.write(prefix+b',\n'.join(batch)+update);batch=[]
    if batch:
        out.write(prefix+b',\n'.join(batch)+update)


def summarize_dump(stream, snapshot_at, work_parent=None):
    """Build from the same immutable native SQL snapshot, replace on success only."""
    now = str(snapshot_at).encode()
    receipt = dict(format=1,scope='backup_only',eligible_cards=0,summarized_sessions=0,
                   summary_rows=0,protected_cards=0,financial_records='complete')
    with tempfile.TemporaryDirectory(prefix='max-backup-sort-',dir=work_parent) as folder:
        (Path(folder)/'.max-backup-owner').write_bytes(b'max-radius-backup-work-v1\n')
        db = sqlite3.connect(str(Path(folder)/'summary.sqlite'))
        try:
            db.executescript('''PRAGMA cache_size=-4096; PRAGMA temp_store=FILE; PRAGMA mmap_size=0;
                PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                CREATE TABLE card(id INTEGER PRIMARY KEY,name BLOB,renewed BLOB,reseller INTEGER,blocked INTEGER DEFAULT 0);
                CREATE INDEX card_name ON card(name);
                CREATE TABLE baseline(id INTEGER PRIMARY KEY);
                CREATE TABLE selected(rid INTEGER PRIMARY KEY,vid INTEGER,uid BLOB,up INTEGER,down INTEGER,seconds INTEGER);
                CREATE TABLE sealed(uid BLOB PRIMARY KEY);
                CREATE TABLE evidence(uid BLOB,nas BLOB,sid BLOB,start BLOB,name BLOB,kind BLOB,PRIMARY KEY(uid,nas,sid,start,name));
                CREATE TABLE seen(vid INTEGER,sid BLOB,PRIMARY KEY(vid,sid));
                CREATE TABLE uid_seen(uid BLOB PRIMARY KEY,vid INTEGER);
                CREATE TABLE groups(k TEXT PRIMARY KEY,vid INTEGER,name BLOB,reseller INTEGER,cycle BLOB,
                    first BLOB,last BLOB,nas BLOB,called BLOB,port BLOB,protocol BLOB,service BLOB,visible INTEGER,
                    up INTEGER,down INTEGER,seconds INTEGER,n INTEGER);''')
            schemas=set()
            # Accounts/baselines must precede eligibility, regardless of dump order.
            for table,cols,raw in _events(stream,TARGETS):
                from services.backup_compaction import CREATE
                m=CREATE.match(raw)
                if m:schemas.add(m[1].decode())
                if table not in ('wisp_vouchers','wisp_session_baselines','wisp_nas_session_seals','wisp_account_usage_seals','wisp_session_service_evidence'):continue
                for _,r in _rows(raw,cols,SUMMARY_COLUMNS):
                    if table=='wisp_session_service_evidence':
                        db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?)',(r.get('acctuniqueid'),r.get('nasipaddress'),r.get('acctsessionid'),r.get('acctstarttime'),r.get('original_username'),r.get('service_type')))
                    elif table=='wisp_account_usage_seals':
                        db.execute('INSERT OR IGNORE INTO sealed VALUES(?)',(r['acctuniqueid'],))
                    elif table in ('wisp_session_baselines','wisp_nas_session_seals'):
                        db.execute('INSERT OR IGNORE INTO baseline VALUES(?)',(_integer(r['radacctid']),))
                    elif r.get('status')==b'expired':
                        db.execute('INSERT INTO card(id,name,renewed,reseller) VALUES(?,?,?,?)',
                                   (_integer(r['id']),r['username'],r.get('last_renewed_at') or r.get('first_used_at') or r.get('created_at'),_integer(r.get('reseller_id'))))
            if not {'wisp_vouchers','wisp_subscribers','wisp_session_baselines'}.issubset(schemas):
                raise ValueError('Missing snapshot ownership contract for usage summaries.')
            # Metadata cache is bounded; blocked flags are finalized in SQL below.
            @lru_cache(maxsize=4096)
            def by_id(vid):return db.execute('SELECT * FROM card WHERE id=?',(vid,)).fetchone()
            @lru_cache(maxsize=4096)
            def by_name(name):return db.execute('SELECT * FROM card WHERE name=?',(name,)).fetchone()
            def owner(r):
                if r.get('account_subscriber_id') is not None:return None
                vid=_integer(r.get('account_voucher_id'))
                if vid is not None:return by_id(vid)
                return by_name(r.get('account_read_username') or r.get('account_username') or r.get('username'))
            # Protect the complete card, including rows in other stores.
            for table,cols,raw in _events(stream,TARGETS):
                if table not in DETAIL_TABLES+('wisp_session_reservations','wisp_subscribers'):continue
                for _,r in _rows(raw,cols,SUMMARY_COLUMNS):
                    if table=='wisp_subscribers':
                        db.execute('UPDATE card SET blocked=1 WHERE name=?',(r['username'],));continue
                    if table=='wisp_session_reservations':
                        if r.get('expires_at') and r['expires_at']>now:
                            db.execute('UPDATE card SET blocked=1 WHERE name=?',(r['username'],))
                        continue
                    card=owner(r)
                    if not card:continue
                    vid,name,cycle,reseller,blocked=card
                    start,end=_date(r.get('acctstarttime')),_date(r.get('acctstoptime'))
                    renewed=_date(cycle)
                    bad=(table!='radacct' or not start or not end or end<start or
                         (r.get('acctterminatecause') or b'').lower() in UNCERTAIN or
                         db.execute('SELECT 1 FROM baseline WHERE id=?',(_integer(r.get('radacctid')),)).fetchone() or
                         any(r.get(k) is None or int(r[k])<0 for k in ('acctinputoctets','acctoutputoctets','acctsessiontime')) or
                         (renewed and start<renewed<end) or not r.get('acctsessionid') or
                         not re.fullmatch(rb'[0-9A-Fa-f]{32}',r.get('acctuniqueid') or b'') or
                         db.execute('SELECT 1 FROM sealed WHERE uid=?',(r.get('acctuniqueid'),)).fetchone())
                    if not bad:
                        other=db.execute('SELECT vid FROM uid_seen WHERE uid=?',(r['acctuniqueid'],)).fetchone()
                        if other:
                            db.execute('UPDATE card SET blocked=1 WHERE id=?',(other[0],));bad=True
                        else:db.execute('INSERT INTO uid_seen VALUES(?,?)',(r['acctuniqueid'],vid))
                    if not bad:
                        try:db.execute('INSERT INTO seen VALUES(?,?)',(vid,r['acctsessionid']))
                        except sqlite3.IntegrityError:bad=True
                    if bad:db.execute('UPDATE card SET blocked=1 WHERE id=?',(vid,))
                    if not bad and table=='radacct':
                        vid,name,cycle,reseller,_=card
                        proof=db.execute('SELECT kind FROM evidence WHERE uid=? AND nas=? AND sid=? AND start=? AND name=?',tuple(r.get(k) for k in ('acctuniqueid','nasipaddress','acctsessionid','acctstarttime','username'))).fetchone()
                        if proof and proof[0]==b'hotspot' and not r.get('servicetype') and (r.get('framedprotocol') or b'').lower()!=b'ppp':r['servicetype']=b'Login-User'
                        if proof and proof[0]==b'pppoe' and not r.get('framedprotocol') and (r.get('servicetype') or b'').lower() not in (b'login-user',b'login'):r['framedprotocol']=b'PPP'
                        start=r['acctstarttime'];scope='current' if not cycle or start>=cycle else 'previous'
                        dims=tuple(r.get(n) or b'' for n in ('nasipaddress','calledstationid','nasportid','framedprotocol','servicetype'))
                        visible=int(r.get('acctterminatecause')!=b'Consolidated-Historical-Import')
                        key=hashlib.sha256(json.dumps([visible,vid,cycle.decode() if cycle else None,scope,start.decode(),
                            *[v.decode('utf8') for v in dims]],ensure_ascii=False).encode()).hexdigest()
                        values=(key,vid,name,reseller,cycle,start,start,*dims,visible,int(r['acctinputoctets']),int(r['acctoutputoctets']),int(r['acctsessiontime']),1)
                        db.execute('''INSERT INTO groups VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(k) DO UPDATE SET
                            first=MIN(first,excluded.first),last=MAX(last,excluded.last),up=up+excluded.up,
                            down=down+excluded.down,seconds=seconds+excluded.seconds,n=n+1''',values)
                        db.execute('INSERT INTO selected VALUES(?,?,?,?,?,?)',(_integer(r['radacctid']),vid,r['acctuniqueid'],int(r['acctinputoctets']),int(r['acctoutputoctets']),int(r['acctsessiontime'])))
            # Cached metadata never decides final eligibility. A late conflicting
            # row protects every staged session/group of that card atomically.
            db.execute('DELETE FROM selected WHERE vid IN (SELECT id FROM card WHERE blocked=1)')
            db.execute('DELETE FROM groups WHERE vid IN (SELECT id FROM card WHERE blocked=1)')
            db.commit()
            receipt.update(eligible_cards=db.execute('SELECT COUNT(*) FROM card WHERE blocked=0').fetchone()[0],
                protected_cards=db.execute('SELECT COUNT(*) FROM card WHERE blocked=1').fetchone()[0],
                summarized_sessions=db.execute('SELECT COUNT(*) FROM selected').fetchone()[0],
                summary_rows=db.execute('SELECT COUNT(*) FROM groups').fetchone()[0])
            with tempfile.TemporaryFile(mode='w+b',dir=folder) as out:
                # Older readers must fail before the first DROP/INSERT rather than
                # restore a quota they cannot read. The current native importer sets
                # this session capability before streaming any user SQL.
                out.write(b'''CREATE TEMPORARY TABLE max_usage_summary_restore_guard (
                    accepted INTEGER NOT NULL CHECK (accepted=1));
INSERT INTO max_usage_summary_restore_guard VALUES(COALESCE(@max_usage_summary_reader_v1,0));
DROP TEMPORARY TABLE max_usage_summary_restore_guard;
''')
                for table,cols,raw in _events(stream,TARGETS):
                    if table not in ('radacct','wisp_traffic_checkpoints'):out.write(raw);continue
                    kept=[v for v,r in _rows(raw,cols,('radacctid',)) if not db.execute('SELECT 1 FROM selected WHERE rid=?',(_integer(r['radacctid']),)).fetchone()]
                    if kept:
                        out.write(('INSERT INTO `'+table+'` ('+','.join('`'+c+'`' for c in cols)+') VALUES\n').encode()+b',\n'.join(kept)+b';\n')
                # Always includes its own schema, including legacy source snapshots.
                out.write(b'\n'+TABLE_SQL.encode()+b';\n')
                emit_rows(out,db.execute('SELECT * FROM groups ORDER BY k'))
                out.write(b'\n'+SEALS_SQL.encode()+b';\n')
                batch=[]
                for row in db.execute('SELECT uid,vid,up,down,seconds FROM selected ORDER BY rid'):
                    batch.append(b'('+b','.join(literal(v) for v in row)+b')')
                    if len(batch)==256:
                        out.write(b'INSERT INTO `wisp_account_usage_seals` VALUES\n'+b',\n'.join(batch)+b';\n');batch=[]
                if batch:out.write(b'INSERT INTO `wisp_account_usage_seals` VALUES\n'+b',\n'.join(batch)+b';\n')
                out.flush();out.seek(0)
                stream.seek(0);stream.truncate();shutil.copyfileobj(out,stream,1024*1024);stream.flush();stream.seek(0)
        finally:db.close()
    return receipt
