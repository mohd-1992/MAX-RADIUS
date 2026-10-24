# -*- coding: utf-8 -*-
"""
Manager, Reseller, Role-Based Access Control (RBAC), and Financial Ledger Service.
Handles role permissions matrix, manager accounts, wallet balances,
cash vs credit ledger invoices, debt repayment, invoice voiding, and statements.
"""

import json
import hashlib
import re
from decimal import Decimal, InvalidOperation
import secrets
import datetime
from core.license_feature_policy import require as _require_feature
from database.db import query_all, query_one, execute_write, execute_update, log_audit, db_session, adapt_query
from core.rbac import hash_manager_password, verify_manager_password



from core.ownership import scoped_query_all as query_all, scoped_query_one as query_one

def _money(value, positive=False):
    try:
        number = Decimal(str(value or 0))
        if not number.is_finite() or abs(number) > Decimal('9999999999.99'):
            raise ValueError()
        number = number.quantize(Decimal('0.01'))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('يرجى إدخال مبلغ مالي صالح.')
    if number < 0 or (positive and number <= 0):
        raise ValueError('المبلغ يجب أن يكون أكبر من الصفر.' if positive else 'القيمة لا تقبل السالب.')
    return float(number)


def _manager_tx(conn, sql, params=(), fetch=None):
    cur = conn.cursor()
    try:
        cur.execute(adapt_query(sql, conn), params)
        if fetch == 'one':
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == 'all':
            return [dict(row) for row in cur.fetchall()]
        return cur.lastrowid
    finally:
        cur.close()


def _permission_ids(permission_ids):
    try:
        ids = sorted({int(p) for p in (permission_ids or [])})
    except (ValueError, TypeError):
        raise ValueError('قائمة الصلاحيات غير صالحة.')
    known = {p['id']: p for p in query_all('SELECT id,code,name FROM wisp_permissions')}
    if not set(ids).issubset(known):
        raise ValueError('تتضمن القائمة صلاحية غير موجودة؛ لم تحفظ التغييرات.')
    from core.permission_catalog import permission_rows
    selected = {known[ident]['code'] for ident in ids}
    dependencies = {row['code']: row['parent'] for row in permission_rows()}
    missing = sorted({dependencies[code] for code in selected
                      if code in dependencies and dependencies[code] not in selected})
    if missing:
        names = {p['code']: p['name'] for p in known.values()}
        raise ValueError('لم تحفظ الصلاحيات؛ اختر المتطلبات الأساسية أيضًا: ' +
                         '، '.join(names.get(code, code) for code in missing))
    return ids


def _validate_manager_data(data, existing=None):
    if not str(data.get('full_name', '')).strip():
        raise ValueError('الاسم الكامل مطلوب.')
    role_id = int(data.get('role_id', (existing or {}).get('role_id', 2)))
    if not get_role_by_id(role_id):
        raise ValueError('الدور المحدد غير موجود.')
    _money(data.get('credit_limit', (existing or {}).get('credit_limit', 0)))
    commission = _money(data.get('commission_percent', (existing or {}).get('commission_percent', 0)))
    if commission > 100:
        raise ValueError('نسبة العمولة يجب أن تكون بين صفر و100.')
    values = data.get('allowed_packages') or []
    if isinstance(values, str):
        values = json.loads(values) if values.startswith('[') else values.split(',')
    try:
        ids = sorted({int(p) for p in values})
    except (ValueError, TypeError):
        raise ValueError('قائمة الباقات غير صالحة.')
    known = {p['id'] for p in query_all('SELECT id FROM wisp_packages')}
    if not set(ids).issubset(known):
        raise ValueError('تتضمن القائمة باقة غير موجودة.')
    return json.dumps(ids) if ids else None


def _active_financial_manager(mgr):
    if not mgr or not mgr.get('is_active') or mgr.get('is_deleted'):
        raise ValueError('الحساب غير موجود أو موقوف أو محذوف؛ لم يتغير الرصيد.')


def _operation_invoice(conn, manager_id, operation, amount, payment_type, request_id, admin_user):
    if not request_id:
        return None, None
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', str(request_id)):
        raise ValueError('مرجع العملية غير صالح؛ حدّث الصفحة.')
    digest = hashlib.sha256(f'{admin_user}:{request_id}'.encode()).hexdigest()[:32]
    number = 'INV-REQ-' + digest
    previous = _manager_tx(conn, 'SELECT * FROM wisp_manager_invoices WHERE invoice_number = ?', (number,), 'one')
    if previous and (previous['manager_id'] != manager_id or previous['transaction_type'] != operation
                     or previous['payment_type'] != payment_type or _money(previous['amount']) != amount):
        raise ValueError('هذا المرجع مستخدم لعملية مختلفة؛ حدّث الصفحة.')
    return number, previous


def _debt_in_transaction(conn, manager_id):
    row = _manager_tx(conn, """SELECT
        COALESCE(SUM(CASE WHEN transaction_type='deposit' AND payment_type='credit' THEN amount ELSE 0 END),0) credit,
        COALESCE(SUM(CASE WHEN transaction_type='debt_payment' THEN amount ELSE 0 END),0) paid
        FROM wisp_manager_invoices WHERE manager_id=? AND COALESCE(is_voided,0)=0""", (manager_id,), 'one')
    return round(max(0, float(row['credit']) - float(row['paid'])), 2)


def transfer_manager_wallet(from_id, to_id, amount, notes='', admin_user='admin', request_id=None):
    from_id, to_id = int(from_id), int(to_id)
    amount = _money(amount, positive=True)
    if from_id == to_id:
        raise ValueError('لا يمكن التحويل إلى الحساب نفسه.')
    with db_session() as conn:
        accounts = {}
        for ident in sorted((from_id, to_id)):
            accounts[ident] = _manager_tx(conn, 'SELECT * FROM wisp_managers WHERE id=? FOR UPDATE', (ident,), 'one')
            _active_financial_manager(accounts[ident])
        number, previous = _operation_invoice(conn, from_id, 'deduction', amount, 'transfer', request_id, admin_user)
        if previous:
            # The paired destination is part of the transfer identity.
            if previous['notes'] != f'تحويل إلى #{to_id}: {notes}':
                raise ValueError('مرجع التحويل مستخدم لمستلم مختلف.')
            return previous['balance_after'], number
        _require_feature('reseller_wallets')
        if float(accounts[from_id]['wallet_balance'] or 0) < amount:
            raise ValueError('رصيد المحفظة غير كافٍ للتحويل.')
        number = number or 'INV-TR-' + secrets.token_hex(16)
        for ident, kind, delta, detail, suffix in (
            (from_id, 'deduction', -amount, f'تحويل إلى #{to_id}: {notes}', ''),
            (to_id, 'deposit', amount, f'تحويل من #{from_id}: {notes}', '-IN')):
            before = float(accounts[ident]['wallet_balance'] or 0)
            after = round(before + delta, 2)
            _manager_tx(conn, 'UPDATE wisp_managers SET wallet_balance=? WHERE id=?', (after, ident))
            _manager_tx(conn, """INSERT INTO wisp_manager_invoices
                (invoice_number,manager_id,transaction_type,amount,payment_type,balance_before,balance_after,notes,is_voided,created_by)
                VALUES(?,?,?,?,'transfer',?,?,?,0,?)""", (number+suffix,ident,kind,amount,before,after,detail,admin_user))
    log_audit(1, admin_user, 'TRANSFER_WALLET', 'managers', f'Transfer reference {number}')
    return round(float(accounts[from_id]['wallet_balance']) - amount, 2), number


# =============================================================================
# 1. Roles & Permissions Management
# =============================================================================

def get_all_permissions_grouped():
    """Returns all available system permissions grouped by category."""
    perms = query_all('SELECT * FROM wisp_permissions ORDER BY id ASC')
    from core.permission_catalog import permission_rows
    dependencies = {row['code']: row['parent'] for row in permission_rows()}
    by_code = {p['code']: p for p in perms}
    own_finance_reads = {'detail.managers.statement', 'detail.managers.invoices',
                        'detail.managers.wallets_view'}
    grouped = {}
    for p in perms:
        parent = by_code.get(dependencies.get(p['code']))
        p['parent_id'] = parent['id'] if parent else ''
        p['parent_name'] = parent['name'] if parent else ''
        code = p['code'].removeprefix('detail.')
        p['administration_only'] = (
            code.startswith(('subscribers.', 'nas.', 'system.', 'roles.')) or
            p['code'] in {'wallets.manage', 'packages.manage', 'managers.manage'} or
            code.startswith('packages.') and p['code'] != 'packages.view' or
            code.startswith('managers.') and p['code'].startswith('detail.') and p['code'] not in own_finance_reads or
            code.startswith('invoices.') and p['code'] not in {'invoices.view', 'detail.invoices.sales_view', 'detail.invoices.sales_export'} or
            'design' in code or code in {'vouchers.bulk_assign_reseller',
                                       'vouchers.action_add_wallet', 'vouchers.action_deduct_wallet'}
        )
        cat = p['category']
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(p)
    return grouped


def get_all_roles():
    """Returns all roles along with the count of assigned managers and permissions."""
    roles = query_all('''
        SELECT r.*,
               (SELECT COUNT(*) FROM wisp_role_permissions WHERE role_id = r.id) as permissions_count,
               (SELECT COUNT(*) FROM wisp_managers WHERE role_id = r.id AND (is_deleted = 0 OR is_deleted IS NULL)) as managers_count
        FROM wisp_roles r
        ORDER BY r.id ASC
    ''')
    return roles


def get_role_by_id(role_id):
    """Returns role details by ID."""
    return query_one('SELECT * FROM wisp_roles WHERE id = ?', (role_id,))


def get_role_permission_ids(role_id):
    """Returns a list of permission IDs assigned to a role."""
    rows = query_all('SELECT permission_id FROM wisp_role_permissions WHERE role_id = ?', (role_id,))
    return [r['permission_id'] for r in rows]


def create_role(data, permission_ids=None, admin_user='admin'):
    name = str(data.get('name', '')).strip()
    code = str(data.get('code', '')).strip() or name.lower().replace(' ', '_')
    if not name or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', code) or code == 'superadmin':
        raise ValueError('اسم الدور أو رمزه غير صالح.')
    pids = _permission_ids(permission_ids)
    with db_session() as conn:
        role_id = _manager_tx(conn, 'INSERT INTO wisp_roles(name,code,description,is_system) VALUES(?,?,?,0)',
                              (name, code, data.get('description', '').strip()))
        for pid in pids:
            _manager_tx(conn, 'INSERT INTO wisp_role_permissions(role_id,permission_id) VALUES(?,?)', (role_id,pid))
    log_audit(1, admin_user, 'CREATE_ROLE', 'roles', f'Created role ID {role_id}')
    return role_id


def update_role(role_id, data, permission_ids=None, admin_user='admin'):
    role = get_role_by_id(role_id)
    if not role:
        raise ValueError('الدور غير موجود.')
    name = str(data.get('name', '')).strip()
    if not name:
        raise ValueError('اسم الدور مطلوب.')
    if data.get('code') and data['code'] != role['code']:
        raise ValueError('رمز الدور ثابت بعد إنشائه.')
    pids = _permission_ids(permission_ids)
    with db_session() as conn:
        _manager_tx(conn, 'SELECT id FROM wisp_roles WHERE id=? FOR UPDATE', (role_id,), 'one')
        _manager_tx(conn, 'UPDATE wisp_roles SET name=?,description=? WHERE id=?', (name,data.get('description','').strip(),role_id))
        _manager_tx(conn, 'DELETE FROM wisp_role_permissions WHERE role_id=?', (role_id,))
        if role.get('is_system') and role['code'] == 'superadmin':
            _manager_tx(conn, 'INSERT INTO wisp_role_permissions(role_id,permission_id) SELECT ?,id FROM wisp_permissions', (role_id,))
        else:
            for pid in pids:
                _manager_tx(conn, 'INSERT INTO wisp_role_permissions(role_id,permission_id) VALUES(?,?)', (role_id,pid))
    log_audit(1, admin_user, 'UPDATE_ROLE', 'roles', f'Updated role ID {role_id}')
    return True


def delete_role(role_id, admin_user='admin'):
    """Deletes a non-system role."""
    role = get_role_by_id(role_id)
    if not role:
        raise ValueError("الدور غير موجود.")
    if role.get('is_system') or role['id'] == 1:
        raise ValueError("لا يمكن حذف الأدوار النظامية الأساسية.")

    managers_count = query_one('SELECT COUNT(*) as c FROM wisp_managers WHERE role_id = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (role_id,))['c']
    if managers_count > 0:
        raise ValueError(f"لا يمكن حذف الدور لأنه مرتبط بـ {managers_count} من المدراء/الموزعين.")

    with db_session() as conn:
        _manager_tx(conn, 'SELECT id FROM wisp_roles WHERE id=? FOR UPDATE', (role_id,), 'one')
        _manager_tx(conn, 'DELETE FROM wisp_role_permissions WHERE role_id = ?', (role_id,))
        _manager_tx(conn, 'DELETE FROM wisp_roles WHERE id = ?', (role_id,))
    log_audit(1, admin_user, 'DELETE_ROLE', 'roles', f'Deleted role {role["name"]} (ID: {role_id})')
    return True


# =============================================================================
# 2. Managers & Resellers Accounts Management
# =============================================================================

def get_all_managers():
    """Returns all non-deleted manager and reseller accounts with their role and calculated net debt."""
    managers = query_all('''
        SELECT m.*, r.name as role_name, r.code as role_code,
               (SELECT COUNT(*) FROM wisp_manager_invoices WHERE manager_id = m.id AND (is_voided = 0 OR is_voided IS NULL)) as invoices_count,
               (SELECT COALESCE(SUM(amount), 0) FROM wisp_manager_invoices WHERE manager_id = m.id AND transaction_type = 'deposit' AND payment_type = 'cash' AND (is_voided = 0 OR is_voided IS NULL)) as total_cash_deposits,
               (SELECT COALESCE(SUM(amount), 0) FROM wisp_manager_invoices WHERE manager_id = m.id AND transaction_type = 'deposit' AND payment_type = 'credit' AND (is_voided = 0 OR is_voided IS NULL)) as total_credit_deposits,
               (SELECT COALESCE(SUM(amount), 0) FROM wisp_manager_invoices WHERE manager_id = m.id AND transaction_type = 'debt_payment' AND (is_voided = 0 OR is_voided IS NULL)) as total_debt_payments
        FROM wisp_managers m
        JOIN wisp_roles r ON m.role_id = r.id
        WHERE (m.is_deleted = 0 OR m.is_deleted IS NULL)
        ORDER BY m.id ASC
    ''')
    for m in managers:
        m.pop('password_hash', None)
        credit = float(m.get('total_credit_deposits') or 0.0)
        repaid = float(m.get('total_debt_payments') or 0.0)
        m['net_outstanding_debt'] = max(0.0, round(credit - repaid, 2))
        
        # Parse allowed_packages
        pkg_val = m.get('allowed_packages')
        if pkg_val:
            if isinstance(pkg_val, str):
                try:
                    m['allowed_packages_list'] = json.loads(pkg_val) if pkg_val.startswith('[') else [int(x.strip()) for x in pkg_val.split(',') if x.strip().isdigit()]
                except Exception:
                    m['allowed_packages_list'] = []
            else:
                m['allowed_packages_list'] = []
        else:
            m['allowed_packages_list'] = []
    return managers


def get_manager_by_id(manager_id):
    """Returns a specific manager by ID."""
    mgr = query_one('''
        SELECT m.*, r.name as role_name, r.code as role_code,
               (SELECT COALESCE(SUM(amount), 0) FROM wisp_manager_invoices WHERE manager_id = m.id AND transaction_type = 'deposit' AND payment_type = 'credit' AND (is_voided = 0 OR is_voided IS NULL)) as total_credit_deposits,
               (SELECT COALESCE(SUM(amount), 0) FROM wisp_manager_invoices WHERE manager_id = m.id AND transaction_type = 'debt_payment' AND (is_voided = 0 OR is_voided IS NULL)) as total_debt_payments
        FROM wisp_managers m
        JOIN wisp_roles r ON m.role_id = r.id
        WHERE m.id = ?
    ''', (manager_id,))
    if mgr:
        credit = float(mgr.get('total_credit_deposits') or 0.0)
        repaid = float(mgr.get('total_debt_payments') or 0.0)
        mgr['net_outstanding_debt'] = max(0.0, round(credit - repaid, 2))
        
        pkg_val = mgr.get('allowed_packages')
        if pkg_val:
            if isinstance(pkg_val, str):
                try:
                    mgr['allowed_packages_list'] = json.loads(pkg_val) if pkg_val.startswith('[') else [int(x.strip()) for x in pkg_val.split(',') if x.strip().isdigit()]
                except Exception:
                    mgr['allowed_packages_list'] = []
            else:
                mgr['allowed_packages_list'] = []
        else:
            mgr['allowed_packages_list'] = []
    return mgr


def create_manager(data, admin_user='admin'):
    from services.license_guard_service import check_manager_quota
    allowed, err_msg, _, _ = check_manager_quota(1)
    if not allowed:
        raise ValueError(err_msg)

    """Creates a new manager or reseller account with package restrictions."""
    allowed_pkgs_str = _validate_manager_data(data)
    username = data['username'].strip()
    if not username or len(str(data.get('password', ''))) < 4:
        raise ValueError('اسم المستخدم وكلمة مرور من أربعة أحرف على الأقل مطلوبان.')
    raw_password = data['password'].strip()
    if len(raw_password) < 4:
        raise ValueError('كلمة المرور يجب ألا تقل عن أربعة أحرف.')
    full_name = data['full_name'].strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    role_id = int(data.get('role_id', 2))
    initial_balance = _money(data.get('wallet_balance', 0.0))
    credit_limit = _money(data.get('credit_limit', 0.0))
    commission = _money(data.get('commission_percent', 0.0))
    payment_type = data.get('initial_payment_type', 'cash')
    notes = data.get('notes', '').strip()

    if role_id!=1:_require_feature('reseller_management')
    if initial_balance>0:_require_feature('reseller_wallets')
    existing = query_one('SELECT id FROM wisp_managers WHERE LOWER(username) = LOWER(?)', (username,))
    if existing:
        raise ValueError(f"اسم المستخدم [{username}] مستخدم بالفعل.")

    if payment_type not in ('cash', 'credit', 'transfer'):
        raise ValueError('نوع الدفع غير صالح.')
    if payment_type == 'credit' and credit_limit > 0 and initial_balance > credit_limit:
        raise ValueError('الرصيد الافتتاحي الآجل يتجاوز سقف الذمة.')
    password_hash = hash_manager_password(raw_password)

    with db_session() as conn:
        manager_id = _manager_tx(conn, '''
            INSERT INTO wisp_managers (
                username, password_hash, full_name, phone, email,
                role_id, wallet_balance, credit_limit, commission_percent,
                allowed_packages, is_active, is_deleted, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
        ''', (
            username, password_hash, full_name, phone, email,
            role_id, initial_balance, credit_limit, commission,
            allowed_pkgs_str, notes
        ))

        # Also sync into wisp_admins for legacy compatibility
        _manager_tx(conn, '''
            INSERT INTO wisp_admins (username, password, full_name, role, is_active)
            VALUES (?, ?, ?, 'manager', 1)
            ON DUPLICATE KEY UPDATE full_name = VALUES(full_name), password = VALUES(password), is_active = 1
        ''', (username, password_hash, full_name))

        # Record initial deposit ledger invoice if balance > 0
        if initial_balance > 0:
            inv_number = f"INV-M{manager_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
            _manager_tx(conn, '''
                INSERT INTO wisp_manager_invoices (
                    invoice_number, manager_id, transaction_type, amount,
                    payment_type, balance_before, balance_after, notes, created_by
                ) VALUES (?, ?, 'deposit', ?, ?, 0.00, ?, ?, ?)
            ''', (
                inv_number, manager_id, initial_balance, payment_type,
                initial_balance, 'رصيد افتتاحي عند إنشاء الحساب', admin_user
            ))

    log_audit(1, admin_user, 'CREATE_MANAGER', 'managers', f'Created manager {username} ({full_name}) with role ID {role_id}')
    return manager_id


def update_manager(manager_id, data, admin_user='admin'):
    """Updates manager details and package restrictions."""
    mgr = get_manager_by_id(manager_id)
    if not mgr:
        raise ValueError("الحساب المطلوب غير موجود.")

    full_name = data['full_name'].strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    role_id = int(data.get('role_id', mgr['role_id']))
    credit_limit = _money(data.get('credit_limit', mgr.get('credit_limit', 0)))
    commission = _money(data.get('commission_percent', mgr.get('commission_percent', 0)))
    notes = data.get('notes', '').strip()
    password = data.get('password', '').strip()

    allowed_pkgs_str = _validate_manager_data(data, mgr)
    if password and len(password) < 4:
        raise ValueError("كلمة المرور يجب ألا تقل عن أربعة أحرف.")

    # If ID 1 (Superadmin), keep role_id = 1
    if manager_id == 1:
        role_id = 1

    if int(mgr['role_id'])!=1 or role_id!=1:_require_feature('reseller_management')
    with db_session() as conn:
        if password:
            password_hash = hash_manager_password(password)
            _manager_tx(conn, '''
                UPDATE wisp_managers SET
                    full_name = ?, phone = ?, email = ?, role_id = ?,
                    credit_limit = ?, commission_percent = ?, allowed_packages = ?,
                    notes = ?, password_hash = ?
                WHERE id = ?
            ''', (full_name, phone, email, role_id, credit_limit, commission, allowed_pkgs_str, notes, password_hash, manager_id))

            _manager_tx(conn, 'UPDATE wisp_admins SET full_name = ?, password = ? WHERE username = ?', (full_name, password_hash, mgr['username']))

        else:
            _manager_tx(conn, '''
                UPDATE wisp_managers SET
                    full_name = ?, phone = ?, email = ?, role_id = ?,
                    credit_limit = ?, commission_percent = ?, allowed_packages = ?, notes = ?
                WHERE id = ?
            ''', (full_name, phone, email, role_id, credit_limit, commission, allowed_pkgs_str, notes, manager_id))

            _manager_tx(conn, 'UPDATE wisp_admins SET full_name = ? WHERE username = ?', (full_name, mgr['username']))

    if password:
        # Sync plain password for SaaS Manager if this is the primary admin account
        if (manager_id == 1 or mgr['username'].lower() in ('admin', 'superadmin')) and password:
            try:
                import os, json
                os.makedirs('/app/storage', exist_ok=True)
                with open('/app/storage/admin_credentials.json', 'w', encoding='utf-8') as f_cred:
                    json.dump({'username': mgr['username'], 'password': password, 'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f_cred, indent=2)
            except Exception:
                pass

    log_audit(1, admin_user, 'UPDATE_MANAGER', 'managers', f'Updated manager {mgr["username"]} (ID: {manager_id})')
    return True


def update_manager_profile(manager_id, full_name, email, phone, current_password=None, new_password=None):
    """Updates manager self-profile with password verification."""
    mgr = get_manager_by_id(manager_id)
    if not mgr:
        raise ValueError("حساب المدير غير موجود.")

    full_name = full_name.strip() if full_name else mgr['full_name']
    email = email.strip() if email else ''
    phone = phone.strip() if phone else ''

    if new_password:
        # Check current password unless user is Super Admin
        if current_password is not None:
            if not verify_manager_password(mgr['password_hash'], current_password):
                raise ValueError("كلمة المرور الحالية غير صحيحة.")

        new_hash = hash_manager_password(new_password)
        execute_write('''
            UPDATE wisp_managers SET full_name = ?, email = ?, phone = ?, password_hash = ?
            WHERE id = ?
        ''', (full_name, email, phone, new_hash, manager_id))
        execute_write('UPDATE wisp_admins SET full_name = ?, password = ? WHERE username = ?', (full_name, new_hash, mgr['username']))
        # Sync plain password for SaaS Manager if this is the primary admin account
        if (manager_id == 1 or mgr['username'].lower() in ('admin', 'superadmin')) and new_password:
            try:
                import os, json
                os.makedirs('/app/storage', exist_ok=True)
                with open('/app/storage/admin_credentials.json', 'w', encoding='utf-8') as f_cred:
                    json.dump({'username': mgr['username'], 'password': new_password, 'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f_cred, indent=2)
            except Exception:
                pass

    else:
        execute_write('''
            UPDATE wisp_managers SET full_name = ?, email = ?, phone = ?
            WHERE id = ?
        ''', (full_name, email, phone, manager_id))
        execute_write('UPDATE wisp_admins SET full_name = ? WHERE username = ?', (full_name, mgr['username']))

    log_audit(manager_id, mgr['username'], 'PROFILE_UPDATE', 'profile', f'Manager {mgr["username"]} updated profile')
    return True


def toggle_manager_status(manager_id, admin_user='admin'):
    """Toggles active/inactive status of a manager account."""
    if manager_id == 1:
        raise ValueError("لا يمكن تعطيل حساب المدير العام الرئيسي.")

    mgr = get_manager_by_id(manager_id)
    if not mgr:
        raise ValueError("الحساب غير موجود.")

    new_status = 0 if mgr['is_active'] else 1
    with db_session() as conn:
        locked = _manager_tx(conn, 'SELECT * FROM wisp_managers WHERE id=? FOR UPDATE', (manager_id,), 'one')
        if not locked or locked.get('is_deleted'):
            raise ValueError('الحساب محذوف ولا يمكن إعادة تفعيله.')
        new_status = 0 if locked['is_active'] else 1
        if new_status and int(locked['role_id'])!=1:_require_feature('reseller_management')
        _manager_tx(conn, 'UPDATE wisp_managers SET is_active = ? WHERE id = ?', (new_status, manager_id))
        _manager_tx(conn, 'UPDATE wisp_admins SET is_active = ? WHERE username = ?', (new_status, mgr['username']))

    status_str = 'تفعيل' if new_status == 1 else 'تعطيل'
    log_audit(1, admin_user, 'TOGGLE_MANAGER', 'managers', f'{status_str} حساب المدير {mgr["username"]}')
    return new_status


def delete_manager(manager_id, admin_user='admin'):
    """
    Safely deactivates and soft-deletes a manager account while preserving historical ledger entries.
    Blocks deletion if the manager has remaining wallet balance, unsettled debt, or active subscribers/cards.
    """
    if manager_id == 1:
        raise ValueError("لا يمكن حذف حساب المدير العام الرئيسي.")

    mgr = get_manager_by_id(manager_id)
    if not mgr or mgr.get('is_deleted'):
        raise ValueError("الحساب غير موجود أو محذوف بالفعل.")

    # 1. Financial Check: Wallet Balance
    wallet_bal = float(mgr.get('wallet_balance') or 0.0)
    if abs(wallet_bal) > 0.001:
        raise ValueError(f"لا يمكن حذف الحساب لوجود رصيد متبقي في المحفظة ({wallet_bal:,.2f}). يرجى تصفية أو سحب الرصيد أولاً.")

    # 2. Financial Check: Unsettled Debt
    net_debt = float(mgr.get('net_outstanding_debt') or 0.0)
    if net_debt > 0.001:
        raise ValueError(f"لا يمكن حذف الحساب لوجود ذمة مالية مستحقة غير مسددة ({net_debt:,.2f}). يرجى تسوية وسداد الذمم أولاً.")

    # 3. Operational Check: Active Vouchers
    active_cards_row = query_one("SELECT COUNT(*) as cnt FROM wisp_vouchers WHERE reseller_id = ? AND status = 'active'", (manager_id,))
    active_cards = active_cards_row['cnt'] if active_cards_row else 0
    if active_cards > 0:
        raise ValueError(f"لا يمكن حذف الحساب لوجود {active_cards} كرت نشط مرتبط به في النظام.")

    # 4. Perform Soft Delete (Preserves all historical invoice ledger rows)
    with db_session() as conn:
        locked = _manager_tx(conn, 'SELECT * FROM wisp_managers WHERE id=? FOR UPDATE', (manager_id,), 'one')
        if not locked or locked.get('is_deleted'):
            raise ValueError('الحساب محذوف بالفعل.')
        active = _manager_tx(conn, "SELECT COUNT(*) c FROM wisp_vouchers WHERE reseller_id=? AND status='active'", (manager_id,), 'one')
        if abs(float(locked.get('wallet_balance') or 0)) > 0.001 or _debt_in_transaction(conn, manager_id) > 0.001 or active['c']:
            raise ValueError('تغيرت أرصدة الحساب أو الكروت النشطة؛ يلزم تسويتها قبل الحذف.')
        _manager_tx(conn, 'UPDATE wisp_managers SET is_deleted = 1, is_active = 0 WHERE id = ?', (manager_id,))
        _manager_tx(conn, 'UPDATE wisp_admins SET is_active = 0 WHERE username = ?', (mgr['username'],))

    log_audit(1, admin_user, 'DELETE_MANAGER', 'managers', f'Soft-deleted manager {mgr["username"]} (ID: {manager_id}) - ledger preserved.')
    return True


# =============================================================================
# 3. Financial Ledger & Statement of Account (كشف الحساب والفواتير والذمم)
# =============================================================================

def deposit_manager_wallet(manager_id, amount, payment_type='cash', notes='', admin_user='admin', request_id=None):
    """
    Adds balance to a manager/reseller wallet with specified payment type (cash نقدي / credit آجل / transfer تحويل).
    Atomically locks the manager row (FOR UPDATE) and inserts a ledger record in wisp_manager_invoices.
    """
    amount = _money(amount, positive=True)
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من الصفر.")

    if payment_type not in ('cash', 'credit', 'transfer'):
        raise ValueError('نوع الدفع غير صالح.')

    with db_session() as conn:
        cursor = conn.cursor()
        sql_lock = adapt_query('SELECT * FROM wisp_managers WHERE id = ? FOR UPDATE', conn)
        cursor.execute(sql_lock, (manager_id,))
        mgr = cursor.fetchone()
        if not mgr:
            raise ValueError("حساب المدير/الموزع غير موجود.")

        _active_financial_manager(mgr)
        operation_number, previous = _operation_invoice(conn, manager_id, 'deposit', amount, payment_type, request_id, admin_user)
        if previous:
            return previous['balance_after'], previous['invoice_number']

        _require_feature('reseller_wallets')
        balance_before = float(mgr['wallet_balance'] or 0.0)
        balance_after = round(balance_before + amount, 2)

        if payment_type == 'credit' and float(mgr.get('credit_limit') or 0) > 0:
            if _debt_in_transaction(conn, manager_id) + amount > float(mgr['credit_limit']):
                raise ValueError('الإيداع الآجل يتجاوز سقف الذمة المسموح.')

        # 1. Update wallet balance
        sql_upd = adapt_query('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', conn)
        cursor.execute(sql_upd, (balance_after, manager_id))

        # 2. Insert ledger invoice
        inv_number = operation_number or f"INV-DEP-{manager_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
        sql_inv = adapt_query('''
            INSERT INTO wisp_manager_invoices (
                invoice_number, manager_id, transaction_type, amount,
                payment_type, balance_before, balance_after, notes, is_voided, created_by
            ) VALUES (?, ?, 'deposit', ?, ?, ?, ?, ?, 0, ?)
        ''', conn)
        cursor.execute(sql_inv, (
            inv_number, manager_id, amount, payment_type,
            balance_before, balance_after, notes or 'إيداع رصيد في المحفظة', admin_user
        ))

    pay_text = 'نقدي' if payment_type == 'cash' else ('آجل (ذمم)' if payment_type == 'credit' else 'تحويل بنكي')
    log_audit(1, admin_user, 'DEPOSIT_WALLET', 'managers', f'Deposited {amount} ({pay_text}) to {mgr["username"]} (New Balance: {balance_after})')
    return balance_after, inv_number


def deduct_manager_wallet(manager_id, amount, notes='', admin_user='admin', request_id=None):
    """
    Deducts balance from a manager/reseller wallet atomically with row locking.
    """
    amount = _money(amount, positive=True)
    if amount <= 0:
        raise ValueError("المبلغ يجب أن يكون أكبر من الصفر.")

    with db_session() as conn:
        cursor = conn.cursor()
        sql_lock = adapt_query('SELECT * FROM wisp_managers WHERE id = ? FOR UPDATE', conn)
        cursor.execute(sql_lock, (manager_id,))
        mgr = cursor.fetchone()
        if not mgr:
            raise ValueError("حساب المدير/الموزع غير موجود.")

        _active_financial_manager(mgr)
        operation_number, previous = _operation_invoice(conn, manager_id, 'deduction', amount, 'cash', request_id, admin_user)
        if previous:
            return previous['balance_after'], previous['invoice_number']

        _require_feature('reseller_wallets')
        balance_before = float(mgr['wallet_balance'] or 0.0)
        if amount > balance_before:
            raise ValueError('الرصيد غير كافٍ للخصم.')
        balance_after = round(balance_before - amount, 2)

        sql_upd = adapt_query('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', conn)
        cursor.execute(sql_upd, (balance_after, manager_id))

        inv_number = operation_number or f"INV-DED-{manager_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
        sql_inv = adapt_query('''
            INSERT INTO wisp_manager_invoices (
                invoice_number, manager_id, transaction_type, amount,
                payment_type, balance_before, balance_after, notes, is_voided, created_by
            ) VALUES (?, ?, 'deduction', ?, 'cash', ?, ?, ?, 0, ?)
        ''', conn)
        cursor.execute(sql_inv, (
            inv_number, manager_id, amount,
            balance_before, balance_after, notes or 'خصم رصيد من المحفظة', admin_user
        ))

    log_audit(1, admin_user, 'DEDUCT_WALLET', 'managers', f'Deducted {amount} from {mgr["username"]} (New Balance: {balance_after})')
    return balance_after, inv_number


def settle_manager_debt(manager_id, amount, payment_type='cash', notes='', admin_user='admin', request_id=None):
    """
    Settles outstanding credit debt (سند قبض / سداد ذمة مالية).
    Reduces the net outstanding debt without inflating spendable wallet balance.
    """
    amount = _money(amount, positive=True)
    if amount <= 0:
        raise ValueError("المبلغ المسدد يجب أن يكون أكبر من الصفر.")

    if payment_type not in ('cash', 'transfer'):
        raise ValueError('نوع الدفع غير صالح.')

    with db_session() as conn:
        cursor = conn.cursor()
        sql_lock = adapt_query('SELECT * FROM wisp_managers WHERE id = ? FOR UPDATE', conn)
        cursor.execute(sql_lock, (manager_id,))
        mgr = cursor.fetchone()
        if not mgr:
            raise ValueError("حساب المدير/الموزع غير موجود.")

        _active_financial_manager(mgr)
        operation_number, previous = _operation_invoice(conn, manager_id, 'debt_payment', amount, payment_type, request_id, admin_user)
        if previous:
            return _debt_in_transaction(conn, manager_id), previous['invoice_number']

        # Calculate current net debt
        sql_debt = adapt_query('''
            SELECT 
                COALESCE(SUM(CASE WHEN transaction_type = 'deposit' AND payment_type = 'credit' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as total_credit,
                COALESCE(SUM(CASE WHEN transaction_type = 'debt_payment' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as total_paid
            FROM wisp_manager_invoices
            WHERE manager_id = ?
        ''', conn)
        cursor.execute(sql_debt, (manager_id,))
        debt_row = cursor.fetchone()
        total_credit = float(debt_row['total_credit'] or 0.0)
        total_paid = float(debt_row['total_paid'] or 0.0)
        outstanding_debt = round(max(0.0, total_credit - total_paid), 2)

        if outstanding_debt <= 0.001:
            raise ValueError("لا توجد ذمم أو مديونيات آجلة مستحقة على هذا الحساب.")

        if amount > outstanding_debt + 0.01:
            raise ValueError(f"المبلغ المدخل ({amount:,.2f}) أكبر من إجمالي الذمة المستحقة ({outstanding_debt:,.2f}).")

        cur_balance = float(mgr['wallet_balance'] or 0.0)
        inv_number = operation_number or f"INV-PAY-{manager_id}-{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"

        sql_inv = adapt_query('''
            INSERT INTO wisp_manager_invoices (
                invoice_number, manager_id, transaction_type, amount,
                payment_type, balance_before, balance_after, notes, is_voided, created_by
            ) VALUES (?, ?, 'debt_payment', ?, ?, ?, ?, ?, 0, ?)
        ''', conn)
        cursor.execute(sql_inv, (
            inv_number, manager_id, amount, payment_type,
            cur_balance, cur_balance, notes or f'سند قبض / سداد ذمة مالية بقيمة {amount}', admin_user
        ))

    remaining_debt = round(max(0.0, outstanding_debt - amount), 2)
    log_audit(1, admin_user, 'SETTLE_DEBT', 'managers', f'Settled {amount} debt for {mgr["username"]} (Remaining Debt: {remaining_debt})')
    return remaining_debt, inv_number


def void_manager_invoice(invoice_id, void_reason, admin_user='admin'):
    """
    Safely voids an existing invoice/ledger entry and atomically reverses its financial impact.
    """
    if not void_reason or not void_reason.strip():
        raise ValueError("يرجى ذكر سبب إلغاء الفاتورة.")

    with db_session() as conn:
        cursor = conn.cursor()
        sql_inv = adapt_query('SELECT * FROM wisp_manager_invoices WHERE id = ? FOR UPDATE', conn)
        cursor.execute(sql_inv, (invoice_id,))
        inv = cursor.fetchone()
        if not inv:
            raise ValueError("الفاتورة المطلوبة غير موجودة.")

        if inv.get('is_voided'):
            raise ValueError("هذه الفاتورة تم إلغاؤها مسبقاً.")

        mgr_id = inv['manager_id']
        tx_type = inv['transaction_type']
        if tx_type == 'card_purchase' or inv.get('payment_type') == 'transfer':
            raise ValueError('لا يمكن عكس شراء الكروت أو أحد طرفي تحويل بقيد منفرد؛ يلزم إجراء تسوية مترابط.')
        amount = float(inv['amount'])

        # Lock manager
        sql_mgr = adapt_query('SELECT id, username, wallet_balance FROM wisp_managers WHERE id = ? FOR UPDATE', conn)
        cursor.execute(sql_mgr, (mgr_id,))
        mgr = cursor.fetchone()
        if not mgr:
            raise ValueError("حساب المدير المرتبط بالفاتورة غير موجود.")

        cur_bal = float(mgr['wallet_balance'] or 0.0)
        new_bal = cur_bal

        # Reversal logic
        if tx_type == 'deposit':
            if amount > cur_bal:
                raise ValueError('لا يكفي الرصيد الحالي لعكس الإيداع؛ لم يلغ القيد.')
            if inv.get('payment_type') == 'credit' and amount > _debt_in_transaction(conn, mgr_id):
                raise ValueError('لا يمكن عكس إيداع آجل تم سداد جزء منه بقيد منفرد.')
            # Deposit gave money -> take it back
            new_bal = round(cur_bal - amount, 2)
            sql_upd = adapt_query('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', conn)
            cursor.execute(sql_upd, (new_bal, mgr_id))
        elif tx_type in ('deduction', 'card_purchase'):
            # Deduction/purchase took money -> refund it
            new_bal = round(cur_bal + amount, 2)
            sql_upd = adapt_query('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', conn)
            cursor.execute(sql_upd, (new_bal, mgr_id))
        elif tx_type == 'refund':
            # Refund added money -> take it back
            new_bal = round(cur_bal - amount, 2)
            sql_upd = adapt_query('UPDATE wisp_managers SET wallet_balance = ? WHERE id = ?', conn)
            cursor.execute(sql_upd, (new_bal, mgr_id))
        elif tx_type == 'debt_payment':
            # Debt payment did not touch wallet, voiding it simply restores the debt.
            pass

        # Mark invoice as voided
        sql_void = adapt_query('''
            UPDATE wisp_manager_invoices
            SET is_voided = 1, void_reason = ?
            WHERE id = ?
        ''', conn)
        cursor.execute(sql_void, (f"تم الإلغاء بواسطة {admin_user}: {void_reason.strip()}", invoice_id))

    log_audit(1, admin_user, 'VOID_INVOICE', 'managers', f'Voided invoice {inv["invoice_number"]} for {mgr["username"]} (Reason: {void_reason})')
    return True


def get_manager_statement(manager_id, date_from=None, date_to=None, payment_type=None, transaction_type=None):
    """
    Generates a comprehensive Statement of Account (كشف حساب تفصيلي) for a specific manager.
    Includes chronological transactions, cash vs credit breakdown, debt payments, and balance progression.
    """
    mgr = get_manager_by_id(manager_id)
    if not mgr:
        return None, [], {}

    sql = "SELECT * FROM wisp_manager_invoices WHERE manager_id = ?"
    params = [manager_id]

    if date_from:
        sql += " AND created_at >= ?"
        params.append(f"{date_from} 00:00:00")
    if date_to:
        sql += " AND created_at <= ?"
        params.append(f"{date_to} 23:59:59")
    if payment_type and payment_type != 'all':
        sql += " AND payment_type = ?"
        params.append(payment_type)
    if transaction_type and transaction_type != 'all':
        sql += " AND transaction_type = ?"
        params.append(transaction_type)

    sql += " ORDER BY id DESC"
    invoices = query_all(sql, params)

    # Summary Statistics (Active non-voided transactions)
    total_cash_deposits = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'deposit' and i['payment_type'] == 'cash' and not i.get('is_voided'))
    total_credit_deposits = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'deposit' and i['payment_type'] == 'credit' and not i.get('is_voided'))
    total_debt_payments = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'debt_payment' and not i.get('is_voided'))
    total_card_purchases = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'card_purchase' and not i.get('is_voided'))
    total_deductions = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'deduction' and not i.get('is_voided'))
    total_refunds = sum(float(i['amount']) for i in invoices if i['transaction_type'] == 'refund' and not i.get('is_voided'))

    net_outstanding_debt = float(mgr.get('net_outstanding_debt') or 0)

    stats = {
        'total_transactions': len(invoices),
        'total_cash_deposits': round(total_cash_deposits, 2),
        'total_credit_deposits': round(total_credit_deposits, 2),
        'total_debt_payments': round(total_debt_payments, 2),
        'net_outstanding_debt': net_outstanding_debt,
        'total_card_purchases': round(total_card_purchases, 2),
        'total_deductions': round(total_deductions, 2),
        'total_refunds': round(total_refunds, 2),
        'total_outflows': round(total_card_purchases + total_deductions, 2),
        'current_balance': float(mgr.get('wallet_balance') or 0.0),
        'credit_limit': float(mgr.get('credit_limit') or 0.0)
    }

    return mgr, invoices, stats


def get_all_manager_invoices(limit=100, manager_id=None, transaction_type=None, payment_type=None, offset=0):
    """Returns general ledger invoices for all managers with optional filters."""
    sql = '''
        SELECT inv.*, m.username, m.full_name, r.name as role_name
        FROM wisp_manager_invoices inv
        JOIN wisp_managers m ON inv.manager_id = m.id
        JOIN wisp_roles r ON m.role_id = r.id
        WHERE 1=1
    '''
    params = []
    if manager_id:
        sql += " AND inv.manager_id = ?"
        params.append(manager_id)
    if transaction_type and transaction_type != 'all':
        sql += " AND inv.transaction_type = ?"
        params.append(transaction_type)
    if payment_type and payment_type != 'all':
        sql += " AND inv.payment_type = ?"
        params.append(payment_type)

    sql += f" ORDER BY inv.id DESC LIMIT {max(1,min(501,int(limit)))} OFFSET {max(0,int(offset))}"
    return query_all(sql, params)


def get_manager_kpis():
    """Returns high level statistics for managers and reseller accounts."""
    counts = query_one('''
        SELECT COUNT(*) as total_managers,
               SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_managers,
               COALESCE(SUM(wallet_balance), 0) as total_wallet_balances,
               COALESCE(SUM(credit_limit), 0) as total_credit_limits
        FROM wisp_managers
        WHERE (is_deleted = 0 OR is_deleted IS NULL)
    ''')
    roles_count = query_one('SELECT COUNT(*) as cnt FROM wisp_roles')['cnt']
    
    invoices_summary = query_one('''
        SELECT 
            COALESCE(SUM(CASE WHEN transaction_type = 'deposit' AND payment_type = 'cash' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as cash_deposits,
            COALESCE(SUM(CASE WHEN transaction_type = 'deposit' AND payment_type = 'credit' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as credit_deposits,
            COALESCE(SUM(CASE WHEN transaction_type = 'debt_payment' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as debt_payments,
            COALESCE(SUM(CASE WHEN transaction_type = 'card_purchase' AND (is_voided = 0 OR is_voided IS NULL) THEN amount ELSE 0 END), 0) as card_purchases
        FROM wisp_manager_invoices
    ''')

    credit_dep = float(invoices_summary['credit_deposits'] if invoices_summary else 0)
    debt_paid = float(invoices_summary['debt_payments'] if invoices_summary else 0)
    net_debt = max(0.0, round(credit_dep - debt_paid, 2))

    return {
        'total_managers': counts['total_managers'] if counts else 0,
        'active_managers': counts['active_managers'] if counts else 0,
        'total_wallet_balances': float(counts['total_wallet_balances'] if counts else 0),
        'total_credit_limits': float(counts['total_credit_limits'] if counts else 0),
        'roles_count': roles_count,
        'cash_deposits': float(invoices_summary['cash_deposits'] if invoices_summary else 0),
        'credit_deposits': credit_dep,
        'debt_payments': debt_paid,
        'net_outstanding_debt': net_debt,
        'card_purchases': float(invoices_summary['card_purchases'] if invoices_summary else 0)
    }


def get_manager_operation_receipt(invoice_id):
    """Read a ledger receipt through the current account ownership scope."""
    return query_one('''SELECT i.*,m.username,m.full_name
        FROM wisp_manager_invoices i LEFT JOIN wisp_managers m ON m.id=i.manager_id
        WHERE i.id=?''', (invoice_id,))
