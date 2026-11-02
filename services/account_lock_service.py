"""One lock namespace per tenant schema, shared by all account operations."""
import hashlib


def account_lock_name(conn, username):
    with conn.cursor() as cur:
        cur.execute('SELECT DATABASE() AS tenant_schema')
        row=cur.fetchone()
    schema=row['tenant_schema']
    if not schema: raise RuntimeError('Account database identity unavailable')
    return 'max-cycle:'+hashlib.sha256((schema+'\0'+username.casefold()).encode()).hexdigest()[:48]
