"""Compatibility names for the canonical manager wallets; no legacy balance migration."""
from services.manager_service import get_all_managers, get_all_manager_invoices, deposit_manager_wallet, transfer_manager_wallet


def get_wallets_overview():
    managers = get_all_managers()
    wallets = [dict(wallet_id=m['id'], reseller_id=m['id'], reseller_name=m['full_name'] or m['username'],
                    phone=m.get('phone'), balance=float(m.get('wallet_balance') or 0),
                    credit_limit=float(m.get('credit_limit') or 0), total_deposits=float(m.get('total_cash_deposits') or 0)+float(m.get('total_credit_deposits') or 0),
                    wallet_status='active' if m.get('is_active') else 'frozen') for m in managers]
    invoices = get_all_manager_invoices(limit=20)
    ledger = [dict(id=i['id'], reseller_id=i['manager_id'], reseller_name=i['full_name'] or i['username'],
                   trx_type=('transfer_out' if i['transaction_type']=='deduction' else 'transfer_in') if i['payment_type']=='transfer' and str(i.get('notes') or '').startswith(('تحويل إلى #','تحويل من #')) else 'withdraw' if i['transaction_type']=='deduction' else 'voucher_purchase' if i['transaction_type']=='card_purchase' else i['transaction_type'],
                   amount=i['amount'], balance_after=i['balance_after'], notes=i['notes'], created_at=i['created_at']) for i in invoices]
    return dict(wallets=wallets, total_liquidity=sum(w['balance'] for w in wallets), wallets_count=len(wallets),
                active_count=sum(w['wallet_status']=='active' for w in wallets), recent_ledger=ledger)


def transfer_reseller_balance(from_reseller_id, to_reseller_id, amount, notes='', admin_user='admin', request_id=None):
    try:
        transfer_manager_wallet(from_reseller_id, to_reseller_id, amount, notes, admin_user, request_id)
        return True, 'تم التحويل بين محافظ المدراء.'
    except (ValueError, TypeError) as exc:
        return False, str(exc)


def deposit_reseller_balance(reseller_id, amount, notes='إيداع نقدي', admin_user='admin', request_id=None):
    try:
        deposit_manager_wallet(int(reseller_id), amount, notes=notes, admin_user=admin_user, request_id=request_id)
        return True, 'تم الإيداع في محفظة المدير.'
    except (ValueError, TypeError) as exc:
        return False, str(exc)
