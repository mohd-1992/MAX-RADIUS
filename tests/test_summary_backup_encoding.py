"""Exact restoration identities and bounded, repetitive backup representation."""
import datetime as dt
import gzip
import hashlib
import io
import json
import random
import unittest

from services.backup_usage_summary import COLS
from services.summary_backup_encoding import emit_rows, group_key, key_parts, literal


def summary(vid=1, start='2026-11-01 01:00:00', **changes):
    r = dict(zip(COLS,('',vid,'fixture-'+str(vid),7,'2026-10-01 00:00:00',start,start,
        '192.0.2.1','HS','ether1','','Login-User',1,123,456,60,1)))
    r.update(changes)
    scope = 'current' if not r['cycle_start'] or start >= r['cycle_start'] else 'previous'
    identity = [r['report_visible'],vid,r['cycle_start'],scope,start,
                *[r[k] for k in COLS[7:12]]]
    r['summary_key'] = hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
    return tuple(r[k] for k in COLS)


def plain_sql(rows, columns=COLS):
    """Independent, previous release's literal SQL representation."""
    out=io.BytesIO()
    rows=list(rows)
    for offset in range(0,len(rows),128):
        out.write(('INSERT INTO wisp_account_usage_summary ('+','.join(columns)+') VALUES\n').encode())
        out.write(b',\n'.join(b'('+b','.join(literal(v) for v in r)+b')' for r in rows[offset:offset+128])+b';\n')
    return out.getvalue()


class Encoding(unittest.TestCase):
    def test_exact_key_bytes_unicode_escaping_and_cycles(self):
        for cycle in (None,'2026-10-01 00:00:00','2026-12-01 00:00:00'):
            r=summary(cycle_start=cycle,calledstationid='برج "أ" \\ ; DROP TABLE x;')
            a,b=key_parts(r)
            self.assertEqual(hashlib.sha256(a+r[5].encode()+b).hexdigest(),r[0])

    def test_legacy_span_or_foreign_key_is_preserved(self):
        for r in (summary(last_start='2026-11-01 02:00:00'),('f'*64,*summary()[1:])):
            self.assertIsNone(key_parts(r))
            out=io.BytesIO();count=emit_rows(out,[r,r],COLS)
            self.assertEqual(count['plain_rows'],2)
            self.assertNotIn(b'JSON_TABLE',out.getvalue())
            self.assertIn(r[0].encode(),out.getvalue())

    def test_metadata_days_owners_and_points_do_not_merge(self):
        base=summary()
        for r in (summary(2),summary(start='2026-11-02 01:00:00'),
                  summary(reseller_id=8),summary(nasipaddress='192.0.2.2'),
                  summary(report_visible=0),summary(framedprotocol='PPP')):
            self.assertNotEqual(group_key(base),group_key(r))

    def test_bounded_blocks_and_integer_payload(self):
        rows=[summary(start='2026-11-01 '+f'{i//3600:02}:{i//60%60:02}:{i%60:02}',
                      input_bytes=2**63-1,output_bytes=2**53+1) for i in range(300)]
        out=io.BytesIO();counts=emit_rows(out,rows,COLS)
        self.assertEqual(counts,dict(packed_rows=300,plain_rows=0,dictionary_blocks=3))
        self.assertIn(str(2**63-1).encode(),out.getvalue())
        self.assertIn(str(2**53+1).encode(),out.getvalue())
        self.assertNotIn(b'e+',out.getvalue())
        self.assertLess(max(map(len,out.getvalue().splitlines())),100000)

    def test_existing_quality_and_nonmerging_insert(self):
        out=io.BytesIO()
        r=[summary(start=f'2026-11-01 0{i}:00:00')+(i,) for i in (1,2)]
        count=emit_rows(out,r,COLS+('unconfirmed_sessions',),merge=False)
        self.assertEqual(count['packed_rows'],2)
        self.assertIn(b'p.unconfirmed',out.getvalue())
        self.assertNotIn(b'ON DUPLICATE',out.getvalue())

    def test_representation_reduces_repeated_metadata_without_day_rounding(self):
        rng=random.Random(1511);rows=[]
        for vid in range(1,401):
            for hour in range(12):
                rows.append(summary(vid,start=f'2026-11-01 {hour:02}:13:17',
                    input_bytes=rng.randrange(10**8),output_bytes=rng.randrange(3*10**9),uptime_seconds=rng.randrange(4000)))
        old=plain_sql(sorted(rows));out=io.BytesIO();counts=emit_rows(out,rows,COLS)
        self.assertEqual(counts['packed_rows'],len(rows))
        self.assertLess(len(gzip.compress(out.getvalue(),mtime=0)),len(gzip.compress(old,mtime=0))*.40)
        self.assertIn(b'11:13:17',out.getvalue())
        self.assertNotIn(b'DATE(',out.getvalue())


if __name__=='__main__':unittest.main()
