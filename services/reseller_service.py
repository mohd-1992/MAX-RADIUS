# -*- coding: utf-8 -*-
"""
Reseller & Point of Sale (POS) service:
Manages prepaid credit wallets, commissions, wholesale packages, and transactions.
"""

from database.db import query_all, query_one, execute_write, log_audit

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

def create_reseller(data, admin_username='admin'):
    res_id = execute_write('''
        INSERT INTO wisp_resellers (name, contact_person, phone, email, balance, commission_percent, allowed_packages, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data['name'].strip(),
        data.get('contact_person', '').strip(),
        data.get('phone', '').strip(),
        data.get('email', '').strip(),
        float(data.get('balance', 0.0)),
        float(data.get('commission_percent', 10.0)),
        data.get('allowed_packages', 'all'),
        data.get('status', 'active')
    ))
    
    if float(data.get('balance', 0.0)) > 0:
        execute_write('''
            INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
            VALUES (?, 'deposit', ?, ?, 'رصيد افتتاحي عند إنشاء الحساب', 'OPENING')
        ''', (res_id, float(data['balance']), float(data['balance'])))
        
    log_audit(1, admin_username, 'CREATE_RESELLER', 'resellers', f'Created reseller {data["name"]}')
    return res_id

def topup_reseller(reseller_id, amount, description='شحن رصيد مالي', admin_username='admin'):
    reseller = query_one('SELECT * FROM wisp_resellers WHERE id = ?', (reseller_id,))
    if not reseller:
        raise ValueError('الموزع غير موجود.')
        
    amount = float(amount)
    new_balance = float(reseller['balance']) + amount
    
    execute_write('UPDATE wisp_resellers SET balance = ? WHERE id = ?', (new_balance, reseller_id))
    execute_write('''
        INSERT INTO wisp_reseller_transactions (reseller_id, type, amount, balance_after, description, reference_id)
        VALUES (?, 'deposit', ?, ?, ?, ?)
    ''', (reseller_id, amount, new_balance, description, 'TOPUP'))
    
    log_audit(1, admin_username, 'TOPUP_RESELLER', 'resellers', f'Topped up reseller {reseller["name"]} with {amount}')
    return new_balance

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
