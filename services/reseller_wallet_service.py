"""
services/reseller_wallet_service.py
-----------------------------------
Reseller Digital Wallet & Financial Balance Transfer Engine for MAX RADIUS.
Manages reseller financial balances, peer-to-peer transfers, deposits, and statement tracking.
"""

from database.db import get_connection

_get_db = get_connection

def ensure_wallet_tables():
    """Ensure reseller wallet columns and transfer ledger tables exist."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            # 1. Ensure balance column in wisp_resellers
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_reseller_wallets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reseller_id INT NOT NULL UNIQUE,
                    balance DECIMAL(12,2) DEFAULT 0.00,
                    credit_limit DECIMAL(12,2) DEFAULT 0.00,
                    total_deposits DECIMAL(12,2) DEFAULT 0.00,
                    total_purchases DECIMAL(12,2) DEFAULT 0.00,
                    status ENUM('active', 'frozen', 'restricted') DEFAULT 'active',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_reseller (reseller_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            # 2. Wallet ledger transactions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_wallet_ledger (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reseller_id INT NOT NULL,
                    trx_type ENUM('deposit', 'withdraw', 'transfer_out', 'transfer_in', 'voucher_purchase') NOT NULL,
                    amount DECIMAL(12,2) NOT NULL,
                    balance_after DECIMAL(12,2) NOT NULL,
                    counterpart_reseller_id INT NULL,
                    notes VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_ledger_reseller (reseller_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            # Auto-create wallet records for existing resellers if missing
            cur.execute("""
                INSERT IGNORE INTO wisp_reseller_wallets (reseller_id, balance)
                SELECT id, 0.00 FROM wisp_resellers
            """)
            db.commit()
    finally:
        db.close()

def get_wallets_overview():
    """Get all reseller wallets, total liquidity, and recent ledger transactions."""
    ensure_wallet_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT 
                    w.id as wallet_id,
                    w.balance,
                    w.credit_limit,
                    w.total_deposits,
                    w.status as wallet_status,
                    r.id as reseller_id,
                    r.name as reseller_name,
                    r.phone,
                    r.contact_person
                FROM wisp_reseller_wallets w
                JOIN wisp_resellers r ON r.id = w.reseller_id
                ORDER BY w.balance DESC
            """)
            wallets = cur.fetchall()

            cur.execute("SELECT COALESCE(SUM(balance), 0) as total_liquidity FROM wisp_reseller_wallets")
            total_liquidity = (cur.fetchone() or {}).get('total_liquidity', 0.0)

            cur.execute("""
                SELECT 
                    l.id,
                    l.reseller_id,
                    r.name as reseller_name,
                    l.trx_type,
                    l.amount,
                    l.balance_after,
                    l.notes,
                    l.created_at
                FROM wisp_wallet_ledger l
                JOIN wisp_resellers r ON r.id = l.reseller_id
                ORDER BY l.id DESC
                LIMIT 20
            """)
            ledger = cur.fetchall()

        return {
            'wallets': wallets,
            'total_liquidity': total_liquidity,
            'wallets_count': len(wallets),
            'recent_ledger': ledger
        }
    finally:
        db.close()

def transfer_reseller_balance(from_reseller_id, to_reseller_id, amount, notes=''):
    """Safely transfer financial balance between two resellers."""
    ensure_wallet_tables()
    db = _get_db()
    amount = float(amount)
    if amount <= 0:
        return False, "المبلغ يجب أن يكون أكبر من الصفر."

    try:
        with db.cursor() as cur:
            # Check sender wallet
            cur.execute("SELECT balance FROM wisp_reseller_wallets WHERE reseller_id = %s FOR UPDATE", (from_reseller_id,))
            sender = cur.fetchone()
            if not sender or float(sender['balance']) < amount:
                return False, "رصيد المحفظة غير كافٍ لإتمام هذا التحويل."

            # Check receiver wallet
            cur.execute("SELECT balance FROM wisp_reseller_wallets WHERE reseller_id = %s FOR UPDATE", (to_reseller_id,))
            receiver = cur.fetchone()
            if not receiver:
                return False, "الموزع المستلم غير موجود."

            sender_new_bal = float(sender['balance']) - amount
            receiver_new_bal = float(receiver['balance']) + amount

            # Update sender
            cur.execute("UPDATE wisp_reseller_wallets SET balance = %s WHERE reseller_id = %s", (sender_new_bal, from_reseller_id))
            cur.execute("""
                INSERT INTO wisp_wallet_ledger (reseller_id, trx_type, amount, balance_after, counterpart_reseller_id, notes)
                VALUES (%s, 'transfer_out', %s, %s, %s, %s)
            """, (from_reseller_id, -amount, sender_new_bal, to_reseller_id, f"تحويل صادر إلى موزع #{to_reseller_id}: {notes}"))

            # Update receiver
            cur.execute("UPDATE wisp_reseller_wallets SET balance = %s WHERE reseller_id = %s", (receiver_new_bal, to_reseller_id))
            cur.execute("""
                INSERT INTO wisp_wallet_ledger (reseller_id, trx_type, amount, balance_after, counterpart_reseller_id, notes)
                VALUES (%s, 'transfer_in', %s, %s, %s, %s)
            """, (to_reseller_id, amount, receiver_new_bal, from_reseller_id, f"تحويل وارد من موزع #{from_reseller_id}: {notes}"))

            db.commit()
            return True, f"تم تحويل مبلغ {amount:,.2f} بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def deposit_reseller_balance(reseller_id, amount, notes='إيداع رصيد نقدي'):
    """Deposit funds into reseller wallet."""
    ensure_wallet_tables()
    db = _get_db()
    amount = float(amount)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT balance FROM wisp_reseller_wallets WHERE reseller_id = %s FOR UPDATE", (reseller_id,))
            row = cur.fetchone()
            if not row:
                return False, "المحفظة غير موجودة."

            new_bal = float(row['balance']) + amount
            cur.execute("""
                UPDATE wisp_reseller_wallets 
                SET balance = %s, total_deposits = total_deposits + %s 
                WHERE reseller_id = %s
            """, (new_bal, amount, reseller_id))

            cur.execute("""
                INSERT INTO wisp_wallet_ledger (reseller_id, trx_type, amount, balance_after, notes)
                VALUES (%s, 'deposit', %s, %s, %s)
            """, (reseller_id, amount, new_bal, notes))

            db.commit()
            return True, f"تم إيداع مبلغ {amount:,.2f} في محفظة الموزع بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()
