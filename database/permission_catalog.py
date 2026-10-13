"""Transactional RBAC metadata extension; no DDL or application imports."""
from core.permission_catalog import permission_rows


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
        for row in permission_rows():
            execute('SELECT id FROM wisp_permissions WHERE code=?', (row['code'],))
            if cursor.fetchone():
                continue
            execute('INSERT INTO wisp_permissions(code,name,category,description) VALUES(?,?,?,?)',
                    (row['code'], row['name'], row['category'], row['description']))
            new_id = cursor.lastrowid
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
