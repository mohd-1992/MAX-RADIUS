"""Strict, import-safe SQL bootstrap parsing; never imports application config.

Only an empty schema receives seed SQL. Existing schemas use the specialized
migrations; startup must not reset defaults, credentials or financial records.
"""
import re


class SchemaInitializationError(RuntimeError):
    pass


def _without_comments(source):
    out=[];i=0;quote=None
    while i<len(source):
        c=source[i]
        if quote:
            out.append(c);i+=1
            if c=='\\' and i<len(source):out.append(source[i]);i+=1
            elif c==quote:
                if i<len(source) and source[i]==quote:out.append(source[i]);i+=1
                else:quote=None
            continue
        if c in "'\"`":quote=c;out.append(c);i+=1;continue
        if c=='#' or (source.startswith('--',i) and (i+2==len(source) or source[i+2].isspace())):
            end=source.find('\n',i);i=len(source) if end<0 else end;continue
        if source.startswith('/*',i):
            end=source.find('*/',i+2)
            if end<0:raise SchemaInitializationError('Unterminated SQL comment')
            body=source[i+2:end]
            if body.startswith('!'):
                out.append(' '+re.sub(r'^!\d*\s*','',body)+' ')
            elif body.startswith('M!') and not body.startswith('M!999999'):
                out.append(' '+re.sub(r'^M!\d*\s*','',body)+' ')
            else:out.append(' ')
            i=end+2;continue
        out.append(c);i+=1
    if quote:raise SchemaInitializationError('Unterminated SQL quote')
    return ''.join(out)


def sql_statements(source):
    """Honor DELIMITER, quoted separators and executable dump comments."""
    source=_without_comments(source);delimiter=';';buffer=[];quote=None;i=0
    while i<len(source):
        if quote is None and (i==0 or source[i-1]=='\n'):
            match=re.match(r'[ \t]*DELIMITER[ \t]+(\S+)[ \t]*(?:\n|$)',source[i:],re.I)
            if match:
                if ''.join(buffer).strip():raise SchemaInitializationError('DELIMITER inside an unfinished statement')
                delimiter=match[1];i+=match.end();continue
        c=source[i]
        if quote:
            buffer.append(c);i+=1
            if c=='\\' and i<len(source):buffer.append(source[i]);i+=1
            elif c==quote:
                if i<len(source) and source[i]==quote:buffer.append(source[i]);i+=1
                else:quote=None
            continue
        if c in "'\"`":quote=c;buffer.append(c);i+=1;continue
        if source.startswith(delimiter,i):
            statement=''.join(buffer).strip();buffer=[];i+=len(delimiter)
            if statement:
                # A dump's original DEFINER must not become a missing account in
                # a fresh installation. Specialized installers own final objects.
                statement=re.sub(r'^(CREATE\s+)DEFINER\s*=\s*`[^`]+`\s*@\s*`[^`]+`\s*',r'\1',statement,flags=re.I)
                yield statement
            continue
        buffer.append(c);i+=1
    if quote or ''.join(buffer).strip():raise SchemaInitializationError('Unterminated SQL statement')


def required_tables(statements):
    result=set()
    for statement in statements:
        match=re.match(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`(\w+)`',statement,re.I)
        if match:result.add(match[1])
    if len(result)<30:raise SchemaInitializationError('Incomplete bootstrap table definition')
    return result


def bootstrap_empty_schema(conn, source):
    statements=list(sql_statements(source));expected=required_tables(statements)
    cur=conn.cursor()
    try:
        cur.execute('SHOW TABLES')
        existing={next(iter(row.values())) for row in cur.fetchall()}
        if existing:
            core={'radacct','radcheck','radreply','radusergroup','wisp_packages',
                  'wisp_vouchers','wisp_voucher_batches','wisp_voucher_sales',
                  'wisp_subscribers','wisp_managers','wisp_roles','wisp_system_settings'}
            if core-existing:
                raise SchemaInitializationError('Partial base schema; explicit repair required before startup')
            return False
        cur.execute('SELECT @@SESSION.foreign_key_checks AS fk_checks, @@SESSION.sql_mode AS sql_mode')
        original=cur.fetchone()
        try:
            for index,statement in enumerate(statements,1):
                try:cur.execute(statement)
                except Exception as exc:
                    raise SchemaInitializationError('Bootstrap statement %d failed; initialization incomplete'%index) from exc
            conn.commit()
        finally:
            # DDL may commit implicitly: do not pretend rollback restores a
            # partially initialized database, or leave caller FK checks disabled.
            cur.execute('SET SESSION foreign_key_checks=%s',(original['fk_checks'],))
            cur.execute('SET SESSION sql_mode=%s',(original['sql_mode'],))
        cur.execute('SHOW TABLES')
        actual={next(iter(row.values())) for row in cur.fetchall()}
        if expected-actual:raise SchemaInitializationError('Required base tables missing after bootstrap')
        return True
    finally:cur.close()
