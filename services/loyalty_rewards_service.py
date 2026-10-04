"""
services/loyalty_rewards_service.py
-----------------------------------
Loyalty Points & Rewards Engine for MAX RADIUS subscribers.
Tracks earned points on recharges, manages customizable redemption rules, 
and awards data, validity, credit balance, and custom bonuses with atomic concurrency protection.
"""

import datetime
from core.time_service import get_db_storage_now
import logging
from database.db import get_connection, is_mysql_conn

logger = logging.getLogger('loyalty_rewards_service')
_get_db = get_connection


def ensure_loyalty_tables():
    """Ensure subscriber points, customizable rewards catalog, and transaction log tables exist and are up to date."""
    db = _get_db()
    if not is_mysql_conn(db):
        return
    try:
        cur = db.cursor()
        try:
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

            # 2. Customizable Rewards catalog
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_loyalty_rewards (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reward_name VARCHAR(120) NOT NULL,
                    points_cost INT NOT NULL,
                    reward_type VARCHAR(50) DEFAULT 'data_bonus_mb',
                    reward_value BIGINT NOT NULL,
                    description VARCHAR(255) NULL,
                    icon VARCHAR(50) DEFAULT 'fa-gift',
                    is_active TINYINT(1) DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 3. Transactions log
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_loyalty_transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    transaction_type VARCHAR(50) NOT NULL,
                    points INT NOT NULL,
                    balance_after INT NOT NULL,
                    notes VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_trx_user (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # Populate default rewards catalog if table is empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_loyalty_rewards")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT INTO wisp_loyalty_rewards (reward_name, points_cost, reward_type, reward_value, description, icon)
                    VALUES 
                    ('باقة بيانات مجانية 1GB', 100, 'data_bonus_mb', 1024, 'استبدال 100 نقطة بحصة 1 جيجابايت إضافية لحسابك', 'fa-wifi'),
                    ('باقة بيانات سوبر 3GB', 250, 'data_bonus_mb', 3072, 'استبدال 250 نقطة بحصة 3 جيجابايت إضافية فائقة السرعة', 'fa-bolt'),
                    ('تمديد الصلاحية 3 أيام', 50, 'validity_days', 3, 'تمديد صلاحية اشتراكك الحالي 3 أيام إضافية مجاناً', 'fa-calendar-plus'),
                    ('رصيد مالي إضافي 500', 300, 'balance_credit', 500, 'إضافة رصيد مالي في حسابك يمكنك استخدامه لتجديد باقتك', 'fa-wallet')
                """)
                db.commit()
        finally:
            cur.close()
    except Exception as e:
        logger.error("Error in ensure_loyalty_tables: %s", e)
    finally:
        db.close()


def calculate_tier(total_earned):
    """Determine tier level based on cumulative points earned."""
    if total_earned >= 5000:
        return 'vip'
    elif total_earned >= 1500:
        return 'gold'
    elif total_earned >= 500:
        return 'silver'
    return 'bronze'


def get_loyalty_overview():
    """Get complete loyalty dashboard data, customizable rewards, top members, and transactions."""
    ensure_loyalty_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            # Overall KPIs
            cur.execute("""
                SELECT 
                    COUNT(*) as total_members, 
                    COALESCE(SUM(points_balance), 0) as total_circulating_points,
                    COALESCE(SUM(total_points_redeemed), 0) as total_redeemed_points
                FROM wisp_loyalty_wallets
            """)
            stats = cur.fetchone() or {'total_members': 0, 'total_circulating_points': 0, 'total_redeemed_points': 0}

            # All Rewards (both active and inactive for admin management)
            cur.execute("SELECT * FROM wisp_loyalty_rewards ORDER BY is_active DESC, points_cost ASC")
            all_rewards = cur.fetchall() or []

            # Active rewards count
            active_rewards_count = sum(1 for r in all_rewards if r.get('is_active') == 1)

            # Top members
            cur.execute("SELECT * FROM wisp_loyalty_wallets ORDER BY points_balance DESC LIMIT 20")
            top_members = cur.fetchall() or []

            # Recent transactions
            cur.execute("SELECT * FROM wisp_loyalty_transactions ORDER BY id DESC LIMIT 50")
            recent_trxs = cur.fetchall() or []

        return {
            'total_members': stats['total_members'],
            'circulating_points': stats['total_circulating_points'],
            'total_redeemed_points': stats['total_redeemed_points'],
            'active_rewards_count': active_rewards_count,
            'rewards_catalog': [r for r in all_rewards if r.get('is_active') == 1],
            'all_rewards': all_rewards,
            'top_members': top_members,
            'recent_transactions': recent_trxs
        }
    finally:
        db.close()


def get_reward_by_id(reward_id):
    """Fetch single reward item by ID."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM wisp_loyalty_rewards WHERE id = %s", (reward_id,))
            return cur.fetchone()
    finally:
        db.close()


def create_reward(reward_name, points_cost, reward_type, reward_value, description='', icon='fa-gift', is_active=1):
    """Add a new customizable reward to the catalog."""
    ensure_loyalty_tables()
    if not reward_name or points_cost <= 0:
        return False, "اسم المكافأة وتكلفة النقاط مطلوبة ويجب أن تكون أكبر من صفر."

    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO wisp_loyalty_rewards 
                (reward_name, points_cost, reward_type, reward_value, description, icon, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (reward_name.strip(), points_cost, reward_type, reward_value, description.strip() if description else '', icon or 'fa-gift', 1 if is_active else 0))
            db.commit()
            return True, f"تم إنشاء المكافأة «{reward_name}» بنجاح!"
    except Exception as e:
        logger.error("Error creating reward: %s", e)
        return False, str(e)
    finally:
        db.close()


def update_reward(reward_id, reward_name, points_cost, reward_type, reward_value, description='', icon='fa-gift', is_active=1):
    """Update an existing reward in the catalog."""
    ensure_loyalty_tables()
    if not reward_id or not reward_name or points_cost <= 0:
        return False, "بيانات المكافأة غير مكتملة."

    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_loyalty_rewards 
                SET reward_name = %s, points_cost = %s, reward_type = %s, reward_value = %s, 
                    description = %s, icon = %s, is_active = %s
                WHERE id = %s
            """, (reward_name.strip(), points_cost, reward_type, reward_value, description.strip() if description else '', icon or 'fa-gift', 1 if is_active else 0, reward_id))
            db.commit()
            return True, "تم حفظ تعديلات المكافأة بنجاح!"
    except Exception as e:
        logger.error("Error updating reward: %s", e)
        return False, str(e)
    finally:
        db.close()


def delete_reward(reward_id):
    """Delete a reward from the catalog."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM wisp_loyalty_rewards WHERE id = %s", (reward_id,))
            db.commit()
            return True, "تم حذف المكافأة بنجاح."
    except Exception as e:
        logger.error("Error deleting reward: %s", e)
        return False, str(e)
    finally:
        db.close()


def toggle_reward_status(reward_id, is_active):
    """Enable or disable a reward without deleting it."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE wisp_loyalty_rewards SET is_active = %s WHERE id = %s", (1 if is_active else 0, reward_id))
            db.commit()
            status_text = "تفعيل" if is_active else "تعطيل"
            return True, f"تم {status_text} المكافأة بنجاح."
    except Exception as e:
        logger.error("Error toggling reward: %s", e)
        return False, str(e)
    finally:
        db.close()


def award_loyalty_points(username, points, reason='Recharge Bonus'):
    """Award or adjust points for a subscriber wallet."""
    ensure_loyalty_tables()
    username = (username or '').strip()
    if not username or points == 0:
        return False, "اسم المشترك وعدد النقاط مطلوبة."

    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT points_balance, total_points_earned FROM wisp_loyalty_wallets WHERE LOWER(username) = LOWER(%s)", (username,))
            row = cur.fetchone()
            if not row:
                if points < 0:
                    return False, "لا يمكن خصم نقاط من مشترك ليس لديه محفظة ولاء بعد."
                new_bal = points
                total_earned = points
                tier = calculate_tier(total_earned)
                cur.execute("""
                    INSERT INTO wisp_loyalty_wallets (username, points_balance, total_points_earned, tier_level)
                    VALUES (%s, %s, %s, %s)
                """, (username, new_bal, total_earned, tier))
            else:
                new_bal = max(0, row['points_balance'] + points)
                total_earned = row['total_points_earned'] + (points if points > 0 else 0)
                tier = calculate_tier(total_earned)
                cur.execute("""
                    UPDATE wisp_loyalty_wallets 
                    SET points_balance = %s, total_points_earned = %s, tier_level = %s
                    WHERE LOWER(username) = LOWER(%s)
                """, (new_bal, total_earned, tier, username))

            trx_type = 'earn' if points > 0 else 'admin_adjustment'
            cur.execute("""
                INSERT INTO wisp_loyalty_transactions (username, transaction_type, points, balance_after, notes)
                VALUES (%s, %s, %s, %s, %s)
            """, (username, trx_type, points, new_bal, reason))
            db.commit()
            action_text = f"منح {points} نقطة" if points > 0 else f"خصم {abs(points)} نقطة"
            return True, f"تم {action_text} للمشترك {username} بنجاح! الرصيد الحالي: {new_bal} نقطة."
    except Exception as e:
        logger.error("Error awarding points: %s", e)
        return False, str(e)
    finally:
        db.close()


def redeem_reward(username, reward_id):
    """
    Redeem a reward for subscriber.
    Guarantees thread-safety and race-condition immunity via atomic DB operation.
    Applies the reward immediately (data quota, validity days, or cash balance) and logs it.
    Updates RADIUS attributes (Expiration, Cleartext-Password, Max-Total-Octets) and account status.
    Never reactivates disabled or suspended accounts.
    """
    ensure_loyalty_tables()
    username = (username or '').strip()
    if not username or not reward_id:
        return False, "بيانات الاستبدال غير صحيحة."

    db = _get_db()
    cur = None
    try:
        cur = db.cursor()
        try:
            is_mysql = is_mysql_conn(db)
            ph = '%s' if is_mysql else '?'

            # 1. Verify global loyalty setting
            cur.execute("SELECT `value` FROM wisp_system_settings WHERE `key` = 'portal_enable_loyalty'")
            setting_row = cur.fetchone()
            if setting_row and not isinstance(setting_row, dict):
                setting_row = dict(setting_row)
            if not setting_row or setting_row.get('value') != '1':
                return False, "عذراً، نظام نقاط ومكافآت الولاء غير مفعّل حالياً."

            # 2. Fetch reward details
            cur.execute(f"SELECT * FROM wisp_loyalty_rewards WHERE id = {ph} AND is_active = 1", (reward_id,))
            reward = cur.fetchone()
            if reward and not isinstance(reward, dict):
                reward = dict(reward)
            if not reward:
                return False, "المكافأة المطلوبة غير متوفرة أو معطلة حالياً."

            cost = reward['points_cost']

            # 3. ATOMIC point deduction: prevents race conditions & negative balances
            cur.execute(f"""
                UPDATE wisp_loyalty_wallets 
                SET points_balance = points_balance - {ph}, 
                    total_points_redeemed = total_points_redeemed + {ph} 
                WHERE LOWER(username) = LOWER({ph}) AND points_balance >= {ph}
            """, (cost, cost, username, cost))

            if cur.rowcount == 0:
                # Deduction failed: balance was insufficient
                cur.execute(f"SELECT points_balance FROM wisp_loyalty_wallets WHERE LOWER(username) = LOWER({ph})", (username,))
                current_w = cur.fetchone()
                if current_w and not isinstance(current_w, dict):
                    current_w = dict(current_w)
                current_bal = current_w['points_balance'] if current_w else 0
                return False, f"رصيد نقاطك الحالي ({current_bal} نقطة) غير كافٍ لاستبدال هذه المكافأة ({cost} نقطة)."

            # 4. Fetch the new balance
            cur.execute(f"SELECT points_balance FROM wisp_loyalty_wallets WHERE LOWER(username) = LOWER({ph})", (username,))
            new_bal_row = cur.fetchone()
            if new_bal_row and not isinstance(new_bal_row, dict):
                new_bal_row = dict(new_bal_row)
            new_balance = new_bal_row['points_balance'] if new_bal_row else 0

            # 5. Check subscriber or voucher entity
            cur.execute(f"""
                SELECT id, username, status, expires_at, password, 'subscriber' as etype FROM wisp_subscribers WHERE LOWER(username) = LOWER({ph})
                UNION ALL
                SELECT id, username, status, expires_at, COALESCE(NULLIF(password, ''), pin_code, username) as password, 'voucher' as etype FROM wisp_vouchers WHERE LOWER(username) = LOWER({ph})
            """, (username, username))
            entity = cur.fetchone()
            if entity and not isinstance(entity, dict):
                entity = dict(entity)

            now_dt = get_db_storage_now().replace(tzinfo=None)
            reward_type = reward.get('reward_type') or 'data_bonus_mb'
            val = reward.get('reward_value', 0)
            applied_details = ""

            if reward_type == 'data_bonus_mb':
                bonus_mb = int(val)
                # Apply extra quota to subscribers & vouchers
                cur.execute(f"UPDATE wisp_subscribers SET extra_quota_mb = COALESCE(extra_quota_mb, 0) + {ph} WHERE LOWER(username) = LOWER({ph})", (bonus_mb, username))
                cur.execute(f"UPDATE wisp_vouchers SET extra_quota_mb = COALESCE(extra_quota_mb, 0) + {ph} WHERE LOWER(username) = LOWER({ph})", (bonus_mb, username))

                # Reactivate if expired due to quota exhaustion, but NEVER if disabled or suspended
                curr_status = entity.get('status') if entity else None
                curr_exp = entity.get('expires_at') if entity else None
                is_time_valid = True
                if curr_exp:
                    try:
                        exp_str = str(curr_exp).replace('T', ' ').split('.')[0]
                        exp_dt = datetime.datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
                        if exp_dt <= now_dt:
                            is_time_valid = False
                    except Exception:
                        pass

                if curr_status == 'expired' and is_time_valid:
                    cur.execute(f"UPDATE wisp_subscribers SET status = 'active' WHERE LOWER(username) = LOWER({ph}) AND status = 'expired'", (username,))
                    cur.execute(f"UPDATE wisp_vouchers SET status = 'active', expire_reason = '' WHERE LOWER(username) = LOWER({ph}) AND status = 'expired'", (username,))

                # Remove any Max-Total-Octets in radcheck so RADIUS allows access
                cur.execute(f"DELETE FROM radcheck WHERE LOWER(username) = LOWER({ph}) AND attribute = 'Max-Total-Octets'", (username,))

                # Ensure Cleartext-Password exists in radcheck if account is not disabled/suspended
                if curr_status not in ('disabled', 'suspended') and entity and entity.get('password'):
                    u_pwd = entity.get('password')
                    cur.execute(f"DELETE FROM radcheck WHERE LOWER(username) = LOWER({ph}) AND attribute = 'Cleartext-Password'", (username,))
                    cur.execute(f"INSERT INTO radcheck (username, attribute, op, value) VALUES ({ph}, 'Cleartext-Password', ':=', {ph})", (username, u_pwd))

                gb_val = round(bonus_mb / 1024, 1) if bonus_mb >= 1024 else bonus_mb
                unit = "GB" if bonus_mb >= 1024 else "MB"
                applied_details = f"تم إضافة {gb_val} {unit} بيانات إضافية لرصيدك فوراً."

            elif reward_type == 'validity_days':
                days = int(val)
                curr_exp = entity.get('expires_at') if entity else None
                start_exp = now_dt
                if curr_exp:
                    try:
                        exp_str = str(curr_exp).replace('T', ' ').split('.')[0]
                        exp_dt = datetime.datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S')
                        if exp_dt > now_dt:
                            start_exp = exp_dt
                    except Exception:
                        pass

                new_exp_dt = start_exp + datetime.timedelta(days=days)
                new_exp_iso = new_exp_dt.strftime('%Y-%m-%d %H:%M:%S')
                new_fr_exp = new_exp_dt.strftime('%d %b %Y %H:%M:%S')

                # Update expiration without reviving administratively disabled/suspended accounts
                cur.execute(f"""
                    UPDATE wisp_subscribers 
                    SET expires_at = {ph},
                        status = CASE WHEN status IN ('disabled', 'suspended') THEN status ELSE 'active' END
                    WHERE LOWER(username) = LOWER({ph})
                """, (new_exp_iso, username))
                cur.execute(f"""
                    UPDATE wisp_vouchers 
                    SET expires_at = {ph},
                        status = CASE WHEN status IN ('disabled', 'suspended') THEN status ELSE 'active' END,
                        expire_reason = CASE WHEN status IN ('disabled', 'suspended') THEN expire_reason ELSE '' END
                    WHERE LOWER(username) = LOWER({ph})
                """, (new_exp_iso, username))

                # Update Expiration in radcheck
                cur.execute(f"DELETE FROM radcheck WHERE LOWER(username) = LOWER({ph}) AND attribute = 'Expiration'", (username,))
                cur.execute(f"INSERT INTO radcheck (username, attribute, op, value) VALUES ({ph}, 'Expiration', ':=', {ph})", (username, new_fr_exp))

                # Ensure Cleartext-Password exists if not disabled/suspended
                curr_status = entity.get('status') if entity else None
                if curr_status not in ('disabled', 'suspended') and entity and entity.get('password'):
                    u_pwd = entity.get('password')
                    cur.execute(f"DELETE FROM radcheck WHERE LOWER(username) = LOWER({ph}) AND attribute = 'Cleartext-Password'", (username,))
                    cur.execute(f"INSERT INTO radcheck (username, attribute, op, value) VALUES ({ph}, 'Cleartext-Password', ':=', {ph})", (username, u_pwd))

                applied_details = f"تم تمديد صلاحية اشتراكك لمدة {days} يوم إضافية."

            elif reward_type == 'balance_credit':
                credit = float(val)
                # Add financial balance
                cur.execute(f"UPDATE wisp_subscribers SET balance = COALESCE(balance, 0) + {ph} WHERE LOWER(username) = LOWER({ph})", (credit, username))
                cur.execute(f"UPDATE wisp_vouchers SET balance = COALESCE(balance, 0) + {ph} WHERE LOWER(username) = LOWER({ph})", (credit, username))
                applied_details = f"تم إضافة {credit:,.2f} رصيد مالي إلى حسابك."

            else: # custom or coupon
                applied_details = f"تم تسجيل طلب المكافأة «{reward['reward_name']}» بنجاح!"

            # 6. Log transaction
            note_text = f"استبدال مكافأة: {reward['reward_name']} ({applied_details})"
            cur.execute(f"""
                INSERT INTO wisp_loyalty_transactions (username, transaction_type, points, balance_after, notes)
                VALUES ({ph}, 'redeem', {ph}, {ph}, {ph})
            """, (username, -cost, new_balance, note_text))

            db.commit()

            # 7. Disconnect active session if any so Mikrotik reloads the newly granted quotas/validity
            try:
                from services.quick_action_service import action_disconnect_user
                action_disconnect_user(username)
            except Exception as d_err:
                logger.debug("CoA disconnect after redemption notice: %s", d_err)

            return True, f"🎉 مبروك! {applied_details} (رصيدك المتبقي: {new_balance} نقطة)"
        finally:
            if cur:
                try:
                    cur.close()
                except Exception:
                    pass
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error("Error in redeem_reward: %s", e)
        return False, f"حدث خطأ أثناء تنفيذ الاستبدال: {str(e)}"
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        db.close()
