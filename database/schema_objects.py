"""Install changed schema objects without locking tables for identical definitions.

DDL still commits implicitly. Call only from schema installation, never from an
account transaction. Existing financial/accounting definitions remain authoritative.
"""
import re


_HEAD = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|TRIGGER)\s+`?(\w+)`?', re.I)
_TOKEN = re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`|/\*.*?\*/|--[^\n]*|\w+|[^\s]", re.S)


def _definition_tokens(statement):
    statement = re.sub(r'\bCREATE\s+OR\s+REPLACE\b', 'CREATE', statement, count=1, flags=re.I)
    statement = re.sub(r'\bDEFINER\s*=\s*(?:`[^`]*`|\w+)\s*@\s*(?:`[^`]*`|\S+)', '',
                       statement, count=1, flags=re.I)
    result = []
    for token in _TOKEN.findall(statement.strip().rstrip(';')):
        if token.startswith(('/*', '--')):
            continue
        if token.startswith(('\"', "'")):
            result.append(token)
        elif token.startswith('`'):
            result.append(token[1:-1].replace('``', '`').lower())
        else:
            result.append(token.lower())
    # MariaDB SHOW CREATE expands RETURNS INT to RETURNS int(11). The display
    # width is not a changed function signature; keep all literal/body tokens.
    for i in range(len(result) - 6, -1, -1):
        if (result[i] == 'returns' and result[i + 1] in
                ('int', 'integer', 'bigint', 'smallint', 'tinyint', 'mediumint')
                and result[i + 2] == '(' and result[i + 3].isdigit()
                and result[i + 4] == ')' and result[i + 5] != 'zerofill'):
            del result[i + 2:i + 5]
    # SHOW CREATE uses CHARSET for the equivalent CHARACTER SET spelling.
    for i in range(len(result) - 2, -1, -1):
        if result[i:i + 2] == ['character', 'set']:
            result[i:i + 2] = ['charset']
    return tuple(result)


def _existing_definition(cur, kind, name):
    try:
        cur.execute(f'SHOW CREATE {kind} `{name}`')
    except Exception as exc:
        if getattr(exc, 'args', ()) and exc.args[0] in (1305, 1360):
            return None
        raise
    row = cur.fetchone()
    if not isinstance(row, dict):
        raise RuntimeError('Schema comparison requires a dictionary cursor')
    value = row.get('Create Function' if kind == 'FUNCTION' else 'SQL Original Statement')
    # A fresh database bootstrap can create root-owned routines. MariaDB may
    # show their existence but hide their definition from the application user.
    # Treat that as a required migration, not as an already verified match:
    # install the authoritative definition with bounded waits, then require a
    # readable exact definition from the new application-owned object below.
    return value or ''


def ensure_schema_object(cur, statement, maria=True):
    """Return whether DDL ran; compare full signature/body and bound lock waits."""
    match = _HEAD.match(statement)
    if not match:
        raise ValueError('Expected a named FUNCTION or TRIGGER definition')
    kind, name = match.group(1).upper(), match.group(2)
    existing = _existing_definition(cur, kind, name)
    if existing is not None and _definition_tokens(existing) == _definition_tokens(statement):
        return False
    cur.execute('SELECT @@SESSION.lock_wait_timeout AS ddl_lock_wait_timeout')
    previous = int(cur.fetchone()['ddl_lock_wait_timeout'])
    try:
        cur.execute('SET SESSION lock_wait_timeout=2')
        if maria:
            ddl = re.sub(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?', 'CREATE OR REPLACE ',
                         statement, count=1, flags=re.I)
        else:
            if existing is not None:
                cur.execute(f'DROP {kind} `{name}`')
            ddl = re.sub(r'^\s*CREATE\s+OR\s+REPLACE\s+', 'CREATE ', statement, count=1, flags=re.I)
        cur.execute(ddl)
        installed = _existing_definition(cur, kind, name)
        if installed is None or _definition_tokens(installed) != _definition_tokens(statement):
            raise RuntimeError(f'Installed {kind} definition did not match: {name}')
        return True
    finally:
        cur.execute(f'SET SESSION lock_wait_timeout={previous}')


def schema_object_matches(cur, statement):
    """Read-only preflight for migrations that must revoke a lease before DDL."""
    match = _HEAD.match(statement)
    if not match:
        raise ValueError('Expected a named FUNCTION or TRIGGER definition')
    existing = _existing_definition(cur, match.group(1).upper(), match.group(2))
    return existing is not None and _definition_tokens(existing) == _definition_tokens(statement)
