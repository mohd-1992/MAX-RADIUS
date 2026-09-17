"""
services/loyalty_rewards_service.py
-----------------------------------
Loyalty Points & Rewards Engine for MAX RADIUS subscribers.
Tracks earned points on recharges, manages redemption rules, and awards data/validity bonuses.
"""

import pymysql
from database.db import get_connection

_get_db = get_connection

def ensure_loyalty_tables():
    """Ensure subscriber points and redemption rules tables exist."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            # 1. Subscriber points wallet
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_loyalty_wallets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    points_balance INT DEFAULT 0,
                    total_points_earned INT DEFAULT 0,
                    total_points_redeemed INT DEFAULT 0,
                    tier_level ENUM('bronze', 'silver', 'gold', 'vip') DEFAULT 'bronze',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_points_user (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            # 2. Rewards catalog
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_loyalty_rewards (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reward_name VARCHAR(120) NOT NULL,
                    points_cost INT NOT NULL,
                    reward_type ENUM('data_bonus_mb', 'validity_days', 'discount_voucher') DEFAULT 'data_bonus_mb',
                    reward_value BIGINT NOT NULL,
                    description VARCHAR(255) NULL,
                    is_active TINYINT(1) DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            # 3. Transactions log
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_loyalty_transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    transaction_type ENUM('earn', 'redeem', 'admin_adjustment') NOT NULL,
                    points INT NOT NULL,
                    balance_after INT NOT NULL,
                    notes VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_trx_user (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            # Populate default rewards catalog if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_loyalty_rewards")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_loyalty_rewards (reward_name, points_cost, reward_type, reward_value, description)
                    VALUES 
                    ('باقة بيانات مجانية 1GB', 100, 'data_bonus_mb', 1024, 'استبدال 100 نقطة بحصة 1 جيجابايت إضافية'),
                    ('باقة بيانات سوبر 3GB', 250, 'data_bonus_mb', 3072, 'استبدال 250 نقطة بحصة 3 جيجابايت إضافية'),
                    ('تمديد الصلاحية 3 أيام', 50, 'validity_days', 3, 'تمديد صلاحية الاشتراك الحالي 3 أيام إضافية')
                """)
            db.commit()
    finally:
        db.close()

def get_loyalty_overview():
    """Get loyalty program overview, top subscribers with points, catalog and stats."""
    ensure_loyalty_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total_members, COALESCE(SUM(points_balance), 0) as total_circulating_points FROM wisp_loyalty_wallets")
            stats = cur.fetchone() or {'total_members': 0, 'total_circulating_points': 0}

            cur.execute("SELECT * FROM wisp_loyalty_rewards WHERE is_active = 1 ORDER BY points_cost ASC")
            rewards = cur.fetchall()

            cur.execute("SELECT * FROM wisp_loyalty_wallets ORDER BY points_balance DESC LIMIT 15")
            top_members = cur.fetchall()

            cur.execute("SELECT * FROM wisp_loyalty_transactions ORDER BY id DESC LIMIT 15")
            recent_trxs = cur.fetchall()

        return {
            'total_members': stats['total_members'],
            'circulating_points': stats['total_circulating_points'],
            'rewards_catalog': rewards,
            'top_members': top_members,
            'recent_transactions': recent_trxs
        }
    finally:
        db.close()

def award_loyalty_points(username, points, reason='Recharge Bonus'):
    """Award points to subscriber wallet."""
    ensure_loyalty_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT points_balance, total_points_earned FROM wisp_loyalty_wallets WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                new_bal = points
                cur.execute("""
                    INSERT INTO wisp_loyalty_wallets (username, points_balance, total_points_earned)
                    VALUES (%s, %s, %s)
                """, (username, points, points))
            else:
                new_bal = row['points_balance'] + points
                cur.execute("""
                    UPDATE wisp_loyalty_wallets 
                    SET points_balance = %s, total_points_earned = total_points_earned + %s
                    WHERE username = %s
                """, (new_bal, points, username))

            cur.execute("""
                INSERT INTO wisp_loyalty_transactions (username, transaction_type, points, balance_after, notes)
                VALUES (%s, 'earn', %s, %s, %s)
            """, (username, points, new_bal, reason))
            db.commit()
            return True, f"تم منح {points} نقطة للمشترك {username} بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def redeem_reward(username, reward_id):
    """Redeem a reward for subscriber, deducting points and granting data/time."""
    ensure_loyalty_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            # Check reward
            cur.execute("SELECT * FROM wisp_loyalty_rewards WHERE id = %s AND is_active = 1", (reward_id,))
            reward = cur.fetchone()
            if not reward:
                return False, "المكافأة المحددة غير متوفرة."

            # Check wallet
            cur.execute("SELECT points_balance FROM wisp_loyalty_wallets WHERE username = %s", (username,))
            wallet = cur.fetchone()
            if not wallet or wallet['points_balance'] < reward['points_cost']:
                return False, "رصيد النقاط غير كافٍ لاستبدال هذه المكافأة."

            new_bal = wallet['points_balance'] - reward['points_cost']

            # Apply reward
            if reward['reward_type'] == 'data_bonus_mb':
                bonus_mb = reward['reward_value']
                cur.execute("UPDATE wisp_subscribers SET extra_quota_mb = extra_quota_mb + %s WHERE username = %s", (bonus_mb, username))
                cur.execute("UPDATE wisp_vouchers SET extra_quota_mb = extra_quota_mb + %s WHERE username = %s", (bonus_mb, username))
            elif reward['reward_type'] == 'validity_days':
                days = reward['reward_value']
                cur.execute("UPDATE wisp_subscribers SET expires_at = DATE_ADD(COALESCE(expires_at, NOW()), INTERVAL %s DAY) WHERE username = %s", (days, username))

            # Update wallet & log
            cur.execute("""
                UPDATE wisp_loyalty_wallets 
                SET points_balance = %s, total_points_redeemed = total_points_redeemed + %s 
                WHERE username = %s
            """, (new_bal, reward['points_cost'], username))

            cur.execute("""
                INSERT INTO wisp_loyalty_transactions (username, transaction_type, points, balance_after, notes)
                VALUES (%s, 'redeem', %s, %s, %s)
            """, (username, -reward['points_cost'], new_bal, f"استبدال مكافأة: {reward['reward_name']}"))

            db.commit()
            return True, f"تم بنجاح استبدال المكافأة ({reward['reward_name']}) وتطبيقها على حسابك!"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()
