"""Compact SQL for exact usage summaries, without a new stored-data format.

Common account/day/point fields are written once. JSON_TABLE reconstructs the
original rows, keys and integer counters during native MariaDB restoration.
Neither the live schema nor the quota/report readers change.
"""
import datetime as dt
import hashlib
import itertools
import json


def literal(value):
    if value is None:
        return b'NULL'
    if isinstance(value, int):
        return str(value).encode()
    if not isinstance(value, bytes):
        value = str(value).encode('utf8')
    return b"'"+value.replace(b'\\',b'\\\\').replace(b"'",b"\\'").replace(b'\0',b'\\0').replace(b'\n',b'\\n').replace(b'\r',b'\\r')+b"'"


def normalize(row):
    result = [v.decode('utf8') if isinstance(v, bytes) else v for v in row]
    for i in (1, 3, 12, 13, 14, 15, 16, 17):
        if i < len(result) and result[i] is not None:
            result[i] = int(result[i])
    return result


def group_key(row):
    """Only rows with identical stored metadata can share an encoding block."""
    r = normalize(row)
    return tuple(r[1:5]+r[7:13]+[r[5][:10]])


def key_parts(row):
    r = normalize(row)
    if r[5] != r[6]:
        return None  # An older multi-start aggregate must remain opaque.
    try:
        dt.datetime.fromisoformat(r[5])
    except (TypeError, ValueError):
        return None
    scope = 'current' if not r[4] or r[5] >= r[4] else 'previous'
    # Match the original Python JSON bytes exactly, including Unicode and spaces.
    encoded = json.dumps([r[12],r[1],r[4],scope,'__MAX_START__',*r[7:12]],
                         ensure_ascii=False).encode('utf8')
    prefix, suffix = encoded.split(b'"__MAX_START__"', 1)
    prefix += b'"'
    suffix = b'"'+suffix
    if hashlib.sha256(prefix+r[5].encode('utf8')+suffix).hexdigest() != r[0]:
        return None  # Preserve foreign/legacy keys rather than inventing identity.
    return prefix, suffix


def emit_rows(out, rows, columns, *, merge=True, block_size=128):
    """Bounded blocks; plain VALUES remains the fallback for legacy rows.

    JSON numbers are read as DECIMAL/BIGINT by the database, never IEEE doubles.
    The source must be ordered by group_key to obtain dictionary compression.
    """
    prefix = ('INSERT INTO `wisp_account_usage_summary` ('+
              ','.join('`'+c+'`' for c in columns)+') ').encode()
    update = b';\n'
    if merge:
        update = b' ON DUPLICATE KEY UPDATE '+b','.join(
            ('`'+k+'`=`'+k+'`+VALUES(`'+k+'`)').encode()
            for k in ('input_bytes','output_bytes','uptime_seconds','session_count'))
        update += b',first_start=LEAST(first_start,VALUES(first_start)),last_start=GREATEST(last_start,VALUES(last_start));\n'
    plain = []
    packed = []
    counts = dict(packed_rows=0, plain_rows=0, dictionary_blocks=0)

    def flush_plain():
        if plain:
            out.write(prefix+b'VALUES\n'+b',\n'.join(plain)+update)
            plain.clear()

    def flush_packed():
        if not packed:
            return
        # Share the SQL projection across many dictionaries as well as sharing
        # account metadata across starts. UNHEX keeps identity bytes independent
        # of the client's/server's connection character set.
        data = literal(json.dumps(packed,ensure_ascii=False,separators=(',',':')).encode())
        select = [b'SHA2(CONCAT(UNHEX(p.key_prefix),p.started,UNHEX(p.key_suffix)),256)',
                  b'p.vid',b'p.username',b'p.reseller',b'p.cycle',b'p.started',b'p.started',
                  b'p.nas',b'p.called',b'p.port',b'p.protocol',b'p.service',b'p.visible',
                  b'p.up',b'p.down',b'p.seconds',b'p.n']
        fields = [('vid','BIGINT'),('username','VARCHAR(64)'),('reseller','BIGINT'),
                  ('cycle','VARCHAR(32)'),('nas','VARCHAR(45)'),('called','VARCHAR(128)'),
                  ('port','VARCHAR(128)'),('protocol','VARCHAR(32)'),('service','VARCHAR(32)'),
                  ('visible','TINYINT'),('key_prefix','VARCHAR(4096)'),('key_suffix','VARCHAR(8192)')]
        definitions = [(name+' '+kind+" PATH '$[0]["+str(i)+"]' ERROR ON ERROR").encode()
                       for i,(name,kind) in enumerate(fields)]
        point_fields = [b"started VARCHAR(32) PATH '$[0]' ERROR ON ERROR"]
        point_fields += [(name+" DECIMAL(30,0) PATH '$["+str(i)+"]' ERROR ON ERROR").encode()
                         for i,name in enumerate(('up','down','seconds','n'),1)]
        if len(columns) == 18:
            select.append(b'p.unconfirmed')
            point_fields.append(b"unconfirmed BIGINT PATH '$[5]' ERROR ON ERROR")
        definitions.append(b"NESTED PATH '$[1][*]' COLUMNS ("+b','.join(point_fields)+b')')
        out.write(prefix+b'SELECT '+b','.join(select)+b' FROM JSON_TABLE('+data+
                  b", '$[*]' COLUMNS ("+b','.join(definitions)+b')) AS p WHERE 1=1'+update)
        packed.clear()

    def emit(block):
        parts = [key_parts(r) for r in block]
        if len(block) < 2 or not parts[0] or any(p != parts[0] for p in parts):
            flush_packed()
            for row in block:
                plain.append(b'('+b','.join(literal(v) for v in row)+b')')
                counts['plain_rows'] += 1
                if len(plain) == block_size:
                    flush_plain()
            return
        flush_plain()
        first = normalize(block[0])
        points = []
        for row in block:
            r = normalize(row)
            points.append([r[5],*r[13:]])
        common = first[1:5]+first[7:13]+[parts[0][0].hex(),parts[0][1].hex()]
        packed.append([common,points])
        counts['packed_rows'] += len(block)
        counts['dictionary_blocks'] += 1
        if len(packed) == block_size:
            flush_packed()

    for _, group in itertools.groupby(rows, group_key):
        block = []
        for row in group:
            block.append(row)
            if len(block) == block_size:
                emit(block)
                block = []
        if block:
            emit(block)
    flush_plain()
    flush_packed()
    return counts
