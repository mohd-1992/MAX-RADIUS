# -*- coding: utf-8 -*-
"""
Reseller & Point of Sale (POS) service:
Manages prepaid credit wallets, commissions, wholesale packages, and transactions.
"""

from core.license_feature_policy import guarded as _license_guarded
from decimal import Decimal, InvalidOperation
from database.db import query_all, query_one, execute_write, log_audit, db_session, execute_in_transaction, is_mysql_conn


def _deposit_amount(value, allow_zero=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal('99999999.99'):
            raise ValueError()
        amount = amount.quantize(Decimal('0.01'))
        if amount == 0 and not allow_zero:
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('المبلغ يجب أن يكون مبلغًا صالحًا أكبر من الصفر.')

def get_resellers():
    resellers = query_all('''
        SELECT r.*,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE reseller_id = r.id) as total_cards,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE reseller_id = r.id AND status = 'unused') as unused_cards,
               (SELECT COUNT(*) FROM wisp_vouchers WHERE reseller_id = r.id AND status = 'active') as active_cards
        FROM wisp_resellers r
        ORDER BY r.id DESC
    ''')
    return resellers

@_license_guarded('reseller_management')
def create_reseller(data, admin_username='admin'):
    opening = _deposit_amount(data.get('balance', 0), allow_zero=True)
    if opening > 0:
        from core.license_feature_policy import require as _require_feature
        _require_feature('reseller_wallets')
    with db_session() as conn:
        res_id = execute_in_transaction(conn, '''
            INSERT INTO wisp_resellers (name, contact_person, phone, email, balance, commission_percent, allowed_packages, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['name'].strip(), data.get('contact_person', '').strip(),
            data.get('phone', '').strip(), data.get('email', '').strip(),
            str(opening), float(data.get('commission_percent', 10.0)),
            data.get('allowed_packages', 'all'), data.get('status', 'active')
        ))
        if opening > 0:
            execute_in_transaction(conn, '''
                INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
                VALUES (?, 'deposit', ?, ?, 'رصيد افتتاحي عند إنشاء الحساب', 'OPENING')
            ''', (res_id, str(opening), str(opening)))
        
    log_audit(1, admin_username, 'CREATE_RESELLER', 'resellers', f'Created reseller {data["name"]}')
    return res_id

@_license_guarded('reseller_wallets')
def topup_reseller(reseller_id, amount, description='شحن رصيد مالي', admin_username='admin'):
    amount = _deposit_amount(amount)
    with db_session() as conn:
        lock = ' FOR UPDATE' if is_mysql_conn(conn) else ''
        reseller = execute_in_transaction(conn, 'SELECT * FROM wisp_resellers WHERE id = ?' + lock, (reseller_id,), fetch='one')
        if not reseller:
            raise ValueError('الموزع غير موجود.')
        new_balance = Decimal(str(reseller['balance'] or 0)) + amount
        if new_balance > Decimal('99999999.99'):
            raise ValueError('الرصيد يتجاوز الحد المسموح.')
        execute_in_transaction(conn, 'UPDATE wisp_resellers SET balance = ? WHERE id = ?', (str(new_balance), reseller_id))
        execute_in_transaction(conn, '''
            INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
            VALUES (?, 'deposit', ?, ?, ?, ?)
        ''', (reseller_id, str(amount), str(new_balance), description, 'TOPUP'))
    
    log_audit(1, admin_username, 'TOPUP_RESELLER', 'resellers', f'Topped up reseller {reseller["name"]} with {amount}')
    return float(new_balance)

def get_reseller_transactions(reseller_id=None, limit=50):
    if reseller_id:
        return query_all('''
            SELECT t.*, r.name as reseller_name
            FROM wisp_reseller_transactions t
            JOIN wisp_resellers r ON t.reseller_id = r.id
            WHERE t.reseller_id = ?
            ORDER BY t.id DESC LIMIT ?
        ''', (reseller_id, limit))
    else:
        return query_all('''
            SELECT t.*, r.name as reseller_name
            FROM wisp_reseller_transactions t
            JOIN wisp_resellers r ON t.reseller_id = r.id
            ORDER BY t.id DESC LIMIT ?
        ''', (limit,))
