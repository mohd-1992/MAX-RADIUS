"""Transactional RBAC metadata extension; no DDL or application imports."""
from core.permission_catalog import base_permission_rows, permission_rows, standard_role_permissions


def install_permission_catalog(conn, mysql=True):
    """Caller commits/rolls back. Serialize on the existing superadmin role.

    Only new permissions inherit legacy grants. Re-running after a revocation
    cannot grant them again. Requires the existing RBAC tables.
    """
    cursor = conn.cursor()
    placeholder = '%s' if mysql else '?'
    def execute(sql, params=()):
        cursor.execute(sql.replace('?', placeholder), params)
    try:
        execute('SELECT id FROM wisp_roles WHERE id=1' + (' FOR UPDATE' if mysql else ''))
        if not cursor.fetchone():
            raise RuntimeError('RBAC superadmin role is missing; catalog was not installed.')
        added = 0
        for row in base_permission_rows() + permission_rows():
            execute('SELECT id FROM wisp_permissions WHERE code=?', (row['code'],))
            if cursor.fetchone():
                continue
            execute('INSERT INTO wisp_permissions(code,name,category,description) VALUES(?,?,?,?)',
                    (row['code'], row['name'], row['category'], row['description']))
            new_id = cursor.lastrowid
            if row.get('parent'):
                execute('''INSERT INTO wisp_role_permissions(role_id,permission_id)
                    SELECT DISTINCT rp.role_id, ? FROM wisp_role_permissions rp
                    JOIN wisp_permissions p ON p.id=rp.permission_id WHERE p.code=?''',
                        (new_id, row['parent']))
            # Role 1 bypasses grants, but its matrix should also show every item.
            execute('SELECT 1 FROM wisp_role_permissions WHERE role_id=1 AND permission_id=?', (new_id,))
            if not cursor.fetchone():
                execute('INSERT INTO wisp_role_permissions(role_id,permission_id) VALUES(1,?)', (new_id,))
            added += 1
        return added
    finally:
        cursor.close()


def install_standard_role_profiles(conn, mysql=True):
    """Explicit fresh-install/operator action; never called on ordinary restarts."""
    cur = conn.cursor()
    placeholder = '%s' if mysql else '?'
    def execute(sql, args=()):
        cur.execute(sql.replace('?', placeholder), args)
    try:
        execute('SELECT id,code FROM wisp_roles WHERE id IN (1,2,3,4) ORDER BY id' +
                (' FOR UPDATE' if mysql else ''))
        rows = cur.fetchall()
        found = {r['id']: r['code'] for r in rows}
        expected = {1: 'superadmin', 2: 'manager', 3: 'main_reseller', 4: 'pos_agent'}
        if found != expected:
            raise RuntimeError('Standard role identity mismatch; no profiles were changed')
        execute('SELECT id,code FROM wisp_permissions')
        catalog = {r['code']: r['id'] for r in cur.fetchall()}
        plans = {ident: standard_role_permissions(code) for ident, code in expected.items() if ident != 1}
        if any(not codes.issubset(catalog) for codes in plans.values()):
            raise RuntimeError('Incomplete permission catalog; no profiles were changed')
        result = {}
        for ident, codes in plans.items():
            execute('DELETE FROM wisp_role_permissions WHERE role_id=?', (ident,))
            for code in sorted(codes):
                execute('INSERT INTO wisp_role_permissions(role_id,permission_id) VALUES(?,?)',
                        (ident, catalog[code]))
            result[ident] = len(codes)
        return result
    finally:
        cur.close()
