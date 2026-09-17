# -*- coding: utf-8 -*-
"""
Database Maintenance, Storage Analysis & Optimization Service for MAX RADIUS.
Handles:
1. High-Performance Live Statistics for Vouchers, Radius tables, Expired cards.
2. Table Size Analyzer (Top 5 key tables with Arabic labels + other tables aggregation).
3. Historical Log Pruning (radacct closed sessions & radpostauth logs by retention days).
4. Table Defragmentation & Optimization (OPTIMIZE TABLE).
5. Deep Engineering Database Audit (Orphaned records, missing limits, status mismatches, zombie sessions).
6. Auto-Cleanup Scheduler integration.
"""

import time
import datetime
from database.db import query_all, query_one, execute_write, execute_update, get_connection

from core.time_service import get_utc_cutoff_str

SYSTEM_RESERVED_USERS = ('healthcheck', 'probe_user', 'admin')

def get_heartbeat_cutoff(minutes=10):
    return get_utc_cutoff_str(minutes)

def get_maintenance_stats():
    """
    Returns real-time counts and metrics for database maintenance (Sub-50ms execution).
    """
    stats = {
        'total_vouchers': 0,
        'expired_total': 0,
        'expired_by_quota': 0,
        'expired_by_time': 0,
        'recharged_vouchers': 0,
        'active_used_vouchers': 0,
        'unused_vouchers': 0,
        'total_subscribers': 0,
        'radcheck_count': 0,
        'radreply_count': 0,
        'radusergroup_count': 0,
        'radacct_count': 0,
        'radpostauth_count': 0,
        'db_size_mb': 0.0
    }
    
    try:
        # 1. Voucher status breakdown
        v_counts = query_all("""
            SELECT status, COUNT(*) as cnt 
            FROM wisp_vouchers 
            GROUP BY status
        """)
        for r in v_counts:
            st = r['status']
            cnt = int(r['cnt'] or 0)
            if st == 'expired':
                stats['expired_total'] = cnt
            elif st == 'recharged':
                stats['recharged_vouchers'] = cnt
            elif st in ('active', 'used'):
                stats['active_used_vouchers'] += cnt
            elif st == 'unused':
                stats['unused_vouchers'] = cnt
        
        stats['total_vouchers'] = sum(int(r['cnt'] or 0) for r in v_counts)
        
        # 2. Breakdown of Expired vouchers: Quota vs Time
        if stats['expired_total'] > 0:
            quota_breakdown = query_one("""
                SELECT 
                    SUM(CASE WHEN expire_reason LIKE ? OR expire_reason LIKE ? THEN 1 ELSE 0 END) as by_quota
                FROM wisp_vouchers 
                WHERE status = 'expired'
            """, ("%البيانات%", "%كوتا%"))
            stats['expired_by_quota'] = int((quota_breakdown and quota_breakdown['by_quota']) or 0)
            stats['expired_by_time'] = max(0, stats['expired_total'] - stats['expired_by_quota'])
        
        # 3. Subscribers
        sub_row = query_one("SELECT COUNT(*) as cnt FROM wisp_subscribers")
        stats['total_subscribers'] = int((sub_row and sub_row['cnt']) or 0)
        
        # 4. FreeRADIUS Table Counts
        rc_row = query_one("SELECT COUNT(*) as cnt FROM radcheck")
        stats['radcheck_count'] = int((rc_row and rc_row['cnt']) or 0)
        
        rr_row = query_one("SELECT COUNT(*) as cnt FROM radreply")
        stats['radreply_count'] = int((rr_row and rr_row['cnt']) or 0)
        
        rg_row = query_one("SELECT COUNT(*) as cnt FROM radusergroup")
        stats['radusergroup_count'] = int((rg_row and rg_row['cnt']) or 0)
        
        ra_row = query_one("SELECT COUNT(*) as cnt FROM radacct")
        stats['radacct_count'] = int((ra_row and ra_row['cnt']) or 0)
        
        rp_row = query_one("SELECT COUNT(*) as cnt FROM radpostauth")
        stats['radpostauth_count'] = int((rp_row and rp_row['cnt']) or 0)
        
        # 5. DB Size Estimation (MySQL / MariaDB)
        try:
            db_sz = query_one("""
                SELECT SUM(data_length + index_length) / 1024 / 1024 as size_mb
                FROM information_schema.TABLES
                WHERE table_schema = DATABASE()
            """)
            if db_sz and db_sz.get('size_mb'):
                stats['db_size_mb'] = round(float(db_sz['size_mb']), 2)
        except Exception:
            pass
            
    except Exception as e:
        print(f"[ERROR] get_maintenance_stats: {str(e)}")
        
    return stats


def get_detailed_table_sizes():
    """
    Queries information_schema.TABLES to return the top 5 key tables with clear Arabic names
    and aggregates all other system tables into a single summary row.
    """
    TABLE_META = {
        'radacct': {
            'arabic_name': 'سجلات جلسات الاستهلاك والمحاسبة',
            'desc': 'تفاصيل جلسات واستهلاك المشتركين وعدادات التحميل',
            'icon': 'fa-chart-line',
            'color': 'amber'
        },
        'wisp_vouchers': {
            'arabic_name': 'كروت واشتراكات الشبكة',
            'desc': 'بيانات الكروت، الصلاحيات، كلمات المرور، وحالات التشغيل',
            'icon': 'fa-ticket',
            'color': 'indigo'
        },
        'radusergroup': {
            'arabic_name': 'مجموعات وباقات المستخدمين',
            'desc': 'ربط المشتركين بالباقات والسرعات في FreeRADIUS',
            'icon': 'fa-layer-group',
            'color': 'purple'
        },
        'radcheck': {
            'arabic_name': 'بيانات المصادقة وكلمات المرور',
            'desc': 'سجلات التحقق وكلمات المرور في سيرفر الراديوس',
            'icon': 'fa-key',
            'color': 'emerald'
        },
        'radpostauth': {
            'arabic_name': 'سجلات محاولات الدخول',
            'desc': 'سجلات عمليات الدخول المقبولة والمرفوضة للهوتسبوت',
            'icon': 'fa-user-shield',
            'color': 'sky'
        }
    }

    try:
        rows = query_all("""
            SELECT 
                table_name,
                COALESCE(table_rows, 0) as table_rows,
                ROUND(data_length / 1024 / 1024, 2) AS data_mb,
                ROUND(index_length / 1024 / 1024, 2) AS index_mb,
                ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_mb
            FROM information_schema.TABLES
            WHERE table_schema = DATABASE()
            ORDER BY (data_length + index_length) DESC
        """)
        
        total_db_mb = sum(float(r['total_mb'] or 0.0) for r in rows) or 0.01
        
        top_tables = []
        other_rows = 0
        other_data_mb = 0.0
        other_index_mb = 0.0
        other_total_mb = 0.0
        other_count = 0
        
        for r in rows:
            tname = r['table_name']
            t_mb = float(r['total_mb'] or 0.0)
            d_mb = float(r['data_mb'] or 0.0)
            i_mb = float(r['index_mb'] or 0.0)
            r_cnt = int(r['table_rows'] or 0)
            
            if tname in TABLE_META:
                meta = TABLE_META[tname]
                pct = round((t_mb / total_db_mb) * 100, 1)
                top_tables.append({
                    'table_name': tname,
                    'arabic_name': meta['arabic_name'],
                    'desc': meta['desc'],
                    'icon': meta['icon'],
                    'color': meta['color'],
                    'table_rows': r_cnt,
                    'data_mb': d_mb,
                    'index_mb': i_mb,
                    'total_mb': t_mb,
                    'percentage': pct
                })
            else:
                other_count += 1
                other_rows += r_cnt
                other_data_mb += d_mb
                other_index_mb += i_mb
                other_total_mb += t_mb
                
        # Sort top tables in preferred priority order
        order_keys = ['radacct', 'wisp_vouchers', 'radusergroup', 'radcheck', 'radpostauth']
        top_tables.sort(key=lambda x: order_keys.index(x['table_name']) if x['table_name'] in order_keys else 99)
        
        # Add summary row for other tables if any exist
        if other_count > 0:
            other_pct = round((other_total_mb / total_db_mb) * 100, 1)
            top_tables.append({
                'table_name': 'other_tables',
                'arabic_name': 'بقية جداول وبيانات النظام الأخرى',
                'desc': f'تشمل {other_count} جدولاً (المبيعات، الموزعين، الإعدادات، النسخ الاحتياطي، المشتركين...)',
                'icon': 'fa-cubes',
                'color': 'slate',
                'table_rows': other_rows,
                'data_mb': round(other_data_mb, 2),
                'index_mb': round(other_index_mb, 2),
                'total_mb': round(other_total_mb, 2),
                'percentage': other_pct
            })
            
        return {
            'success': True,
            'total_db_mb': round(total_db_mb, 2),
            'table_count': len(rows),
            'tables': top_tables
        }
    except Exception as e:
        print(f"[ERROR] get_detailed_table_sizes: {str(e)}")
        return {'success': False, 'message': str(e), 'tables': [], 'total_db_mb': 0.0}


def prune_historical_logs(days=30, prune_postauth=True, prune_audit=False):
    """
    Prunes closed accounting sessions (radacct) and auth logs (radpostauth) older than the specified days.
    """
    start_t = time.time()
    result = {
        'success': True,
        'retention_days': int(days),
        'deleted_radacct': 0,
        'deleted_radpostauth': 0,
        'deleted_audit_logs': 0,
        'duration_seconds': 0.0,
        'message': ''
    }
    
    try:
        days = int(days)
        if days < 1:
            days = 30
            
        # 1. Prune closed sessions from radacct
        ra_del = execute_update("""
            DELETE FROM radacct 
            WHERE acctstoptime IS NOT NULL 
              AND acctstoptime < DATE_SUB(NOW(), INTERVAL ? DAY)
        """, (days,))
        result['deleted_radacct'] = ra_del or 0
        
        # 2. Prune radpostauth logs
        if prune_postauth:
            rp_del = execute_update("""
                DELETE FROM radpostauth 
                WHERE authdate < DATE_SUB(NOW(), INTERVAL ? DAY)
            """, (days,))
            result['deleted_radpostauth'] = rp_del or 0
            
        # 3. Optional Audit logs
        if prune_audit:
            al_del = execute_update("""
                DELETE FROM wisp_audit_logs 
                WHERE created_at < DATE_SUB(NOW(), INTERVAL ? DAY)
            """, (days,))
            result['deleted_audit_logs'] = al_del or 0
            
        result['duration_seconds'] = round(time.time() - start_t, 2)
        result['message'] = (
            f"تم تنظيف السجلات الأقدم من {days} يوماً بنجاح: "
            f"({result['deleted_radacct']:,} جلسة محاسبة من radacct، "
            + (f"{result['deleted_radpostauth']:,} سجل مصادقة من radpostauth" if prune_postauth else "")
            + f") خلال {result['duration_seconds']} ثانية."
        )
    except Exception as e:
        result['success'] = False
        result['message'] = f"خطأ أثناء تنظيف السجلات: {str(e)}"
        
    return result


def optimize_database_tables(tables=None):
    """
    Runs OPTIMIZE TABLE on database tables to rebuild InnoDB tablespaces,
    defragment index structures, and reclaim unused disk space.
    """
    start_t = time.time()
    if not tables:
        tables = [
            'radacct', 'radpostauth', 'radcheck', 'radusergroup', 
            'wisp_vouchers', 'wisp_voucher_sales', 'wisp_subscribers', 'wisp_audit_logs'
        ]
        
    before_stats = get_detailed_table_sizes()
    before_mb = before_stats.get('total_db_mb', 0.0)
    
    optimized_results = []
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        for tbl in tables:
            t0 = time.time()
            try:
                # OPTIMIZE TABLE
                cursor.execute(f"OPTIMIZE TABLE {tbl}")
                res = cursor.fetchall()
                t_elapsed = round(time.time() - t0, 2)
                optimized_results.append({
                    'table': tbl,
                    'status': 'OK',
                    'duration_s': t_elapsed
                })
            except Exception as e_tbl:
                optimized_results.append({
                    'table': tbl,
                    'status': f'Error: {str(e_tbl)}',
                    'duration_s': round(time.time() - t0, 2)
                })
                
        conn.close()
        
        after_stats = get_detailed_table_sizes()
        after_mb = after_stats.get('total_db_mb', 0.0)
        freed_mb = max(0.0, round(before_mb - after_mb, 2))
        elapsed = round(time.time() - start_t, 2)
        
        return {
            'success': True,
            'before_mb': before_mb,
            'after_mb': after_mb,
            'freed_mb': freed_mb,
            'duration_seconds': elapsed,
            'details': optimized_results,
            'message': f"تم تحسين وإلغاء تجزئة {len(tables)} جداول بنجاح خلال {elapsed} ثانية. (الحجم الحالي: {after_mb} MB)."
        }
        
    except Exception as e:
        return {
            'success': False,
            'message': f"خطأ أثناء تحسين الجداول: {str(e)}",
            'duration_seconds': round(time.time() - start_t, 2)
        }


def run_auto_maintenance_job(retention_days=90):
    """
    Weekly automated cleanup job executed by Watchdog/Scheduler.
    Prunes historical logs older than 90 days and logs the event.
    """
    try:
        print(f"[Auto-Cleanup] Starting weekly database log pruning (Retention: {retention_days} days)...")
        prune_res = prune_historical_logs(days=retention_days, prune_postauth=True, prune_audit=False)
        
        if prune_res.get('deleted_radacct', 0) > 0 or prune_res.get('deleted_radpostauth', 0) > 0:
            # Optimize key log tables to reclaim space
            optimize_res = optimize_database_tables(tables=['radacct', 'radpostauth'])
            print(f"[Auto-Cleanup] Completed: {prune_res['message']} | {optimize_res['message']}")
            
            # Log audit
            execute_write("""
                INSERT INTO wisp_audit_logs (admin_id, username, action, module, details, ip_address)
                VALUES (1, 'system_scheduler', 'AUTO_DB_CLEANUP', 'maintenance', ?, '127.0.0.1')
            """, (f"Auto pruned {prune_res['deleted_radacct']} sessions and {prune_res['deleted_radpostauth']} auth logs. Freed {optimize_res.get('freed_mb', 0)} MB",))
            
            return True
        else:
            print("[Auto-Cleanup] No historical logs required pruning.")
            return False
            
    except Exception as e:
        print(f"[ERROR] run_auto_maintenance_job: {str(e)}")
        return False


def delete_expired_vouchers(delete_type='all', delete_acct=False, batch_limit=50000):
    """
    Cascades deletion of expired vouchers across all related tables:
    - wisp_vouchers
    - radcheck
    - radreply
    - radusergroup
    - radacct (if delete_acct is True)
    """
    start_t = time.time()
    result = {
        'success': True,
        'delete_type': delete_type,
        'deleted_vouchers': 0,
        'deleted_radcheck': 0,
        'deleted_radusergroup': 0,
        'deleted_radreply': 0,
        'deleted_radacct': 0,
        'duration_seconds': 0.0,
        'message': ''
    }
    
    try:
        # Determine target usernames based on delete_type
        if delete_type == 'quota':
            target_users_query = """
                SELECT v.username FROM wisp_vouchers v
                WHERE v.status = 'expired'
                  AND (
                      v.expire_reason LIKE ? 
                      OR v.expire_reason LIKE ? 
                      OR v.expire_reason LIKE ?
                  )
                LIMIT ?
            """
            target_rows = query_all(target_users_query, ("%البيانات%", "%كوتا%", "%quota%", batch_limit))
        elif delete_type == 'time':
            target_users_query = """
                SELECT v.username FROM wisp_vouchers v
                WHERE v.status = 'expired'
                  AND (
                      v.expire_reason LIKE ? 
                      OR v.expire_reason LIKE ? 
                      OR v.expire_reason LIKE ?
                      OR v.expire_reason IS NULL
                      OR (v.expires_at IS NOT NULL AND v.expires_at <= CURRENT_TIMESTAMP)
                  )
                  AND (v.expire_reason NOT LIKE ? AND v.expire_reason NOT LIKE ?)
                LIMIT ?
            """
            target_rows = query_all(target_users_query, (
                "%الصلاحية%", "%الوقت%", "%time%", "%البيانات%", "%كوتا%", batch_limit
            ))
        else: # 'all'
            target_users_query = """
                SELECT v.username FROM wisp_vouchers v
                WHERE v.status = 'expired'
                LIMIT ?
            """
            target_rows = query_all(target_users_query, (batch_limit,))
            
        usernames = [r['username'] for r in target_rows if r.get('username')]
        
        if not usernames:
            result['message'] = 'لم يتم العثور على أي كروت منتهية مطابقة للشروط المحددة.'
            result['duration_seconds'] = round(time.time() - start_t, 2)
            return result
            
        # Process in chunks of 500 for optimal memory and SQL efficiency
        chunk_size = 500
        for i in range(0, len(usernames), chunk_size):
            chunk = usernames[i:i + chunk_size]
            placeholders = ','.join(['?'] * len(chunk))
            
            # 1. Cascade radcheck
            rc_del = execute_update(f'DELETE FROM radcheck WHERE username IN ({placeholders})', tuple(chunk))
            result['deleted_radcheck'] += rc_del or 0
            
            # 2. Cascade radreply
            rr_del = execute_update(f'DELETE FROM radreply WHERE username IN ({placeholders})', tuple(chunk))
            result['deleted_radreply'] += rr_del or 0
            
            # 3. Cascade radusergroup
            rg_del = execute_update(f'DELETE FROM radusergroup WHERE username IN ({placeholders})', tuple(chunk))
            result['deleted_radusergroup'] += rg_del or 0
            
            # 4. Cascade radacct (Optional)
            if delete_acct:
                ra_del = execute_update(f'DELETE FROM radacct WHERE username IN ({placeholders})', tuple(chunk))
                result['deleted_radacct'] += ra_del or 0
                
            # 5. Delete from wisp_vouchers
            wv_del = execute_update(f'DELETE FROM wisp_vouchers WHERE username IN ({placeholders})', tuple(chunk))
            result['deleted_vouchers'] += wv_del or 0
            
        result['duration_seconds'] = round(time.time() - start_t, 2)
        result['message'] = (
            f"تم حذف {result['deleted_vouchers']} كرت منتهي جذرياً وتطهير "
            f"({result['deleted_radcheck']} في radcheck، {result['deleted_radusergroup']} في radusergroup"
            + (f"، {result['deleted_radacct']} جلسة محاسبة" if delete_acct else "")
            + f") خلال {result['duration_seconds']} ثانية بنجاح."
        )
        
    except Exception as e:
        result['success'] = False
        result['message'] = f"حدث خطأ أثناء عملية الحذف: {str(e)}"
        
    return result


def run_database_audit():
    """
    Deep Engineering Database Audit Engine.
    Discovers:
    1. Orphaned FreeRADIUS Records (exists in radcheck/radusergroup but missing from wisp_vouchers & wisp_subscribers).
    2. Missing Auth / Limits (Active/Used/Unused vouchers with missing radcheck/radusergroup records).
    3. Status Mismatch (Expired, Recharged, or Disabled in web DB, but still present in radcheck).
    4. Stale Zombie Accounting Sessions (acctstoptime IS NULL with heartbeat > 10m ago).
    """
    start_t = time.time()
    audit_results = {
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_issues': 0,
        'orphaned_count': 0,
        'missing_limits_count': 0,
        'status_mismatch_count': 0,
        'stale_sessions_count': 0,
        'issues': [],
        'duration_seconds': 0.0
    }
    
    try:
        # -------------------------------------------------------------
        # 1. Orphaned FreeRADIUS Records
        # -------------------------------------------------------------
        orphans = query_all("""
            SELECT DISTINCT r.username, r.attribute, r.value 
            FROM radcheck r
            LEFT JOIN wisp_vouchers v ON r.username = v.username
            LEFT JOIN wisp_subscribers s ON r.username = s.username
            WHERE v.id IS NULL 
              AND s.id IS NULL
              AND r.username NOT IN ('healthcheck', 'probe_user', 'admin')
            LIMIT 200
        """)
        for row in orphans:
            audit_results['issues'].append({
                'id': f"orphan_{row['username']}",
                'username': row['username'],
                'category': 'orphaned_radius',
                'category_label': 'كرت يتيم (Orphaned)',
                'severity': 'warning',
                'details': f"موجود في جدول radcheck بقيمة ({row['attribute']}) ولكن غير موجود في الكروت أو المشتركين.",
                'suggested_fix': 'حذف السجل اليتيم من FreeRADIUS',
                'action_code': 'purge_orphan'
            })
            
        audit_results['orphaned_count'] = len(orphans)
        
        # -------------------------------------------------------------
        # 2. Missing Auth / Limits in FreeRADIUS
        # -------------------------------------------------------------
        missing_auth = query_all("""
            SELECT v.id, v.username, v.status, p.name as package_name,
                   rc.id as rc_id, rg.id as rg_id
            FROM wisp_vouchers v
            JOIN wisp_packages p ON v.package_id = p.id
            LEFT JOIN radcheck rc ON v.username = rc.username AND rc.attribute = 'Cleartext-Password'
            LEFT JOIN radusergroup rg ON v.username = rg.username
            WHERE v.status IN ('active', 'used', 'unused')
              AND (v.expires_at IS NULL OR v.expires_at > CURRENT_TIMESTAMP)
              AND (rc.id IS NULL OR rg.id IS NULL)
            LIMIT 200
        """)
        for row in missing_auth:
            missing_parts = []
            if not row['rc_id']:
                missing_parts.append("كلمة المرور في radcheck")
            if not row['rg_id']:
                missing_parts.append(f"مجموعة الباقة ({row['package_name']}) في radusergroup")
            
            audit_results['issues'].append({
                'id': f"missing_{row['username']}",
                'username': row['username'],
                'category': 'missing_limits',
                'category_label': 'غياب قيود المصادقة',
                'severity': 'danger',
                'details': f"الكرت بحالة ({row['status']}) ولكنه يفتقد إلى: {', '.join(missing_parts)}.",
                'suggested_fix': 'إعادة مزامنة وإنشاء قيود الكرت في FreeRADIUS',
                'action_code': 'sync_auth_limits'
            })
            
        audit_results['missing_limits_count'] = len(missing_auth)
        
        # -------------------------------------------------------------
        # 3. Status Mismatches (Expired/Recharged/Disabled lingering in radcheck)
        # -------------------------------------------------------------
        mismatches = query_all("""
            SELECT v.id, v.username, v.status, v.expire_reason, rc.attribute, rc.value
            FROM wisp_vouchers v
            JOIN radcheck rc ON v.username = rc.username AND rc.attribute = 'Cleartext-Password'
            WHERE v.status IN ('expired', 'recharged', 'disabled')
            LIMIT 200
        """)
        for row in mismatches:
            audit_results['issues'].append({
                'id': f"mismatch_{row['username']}",
                'username': row['username'],
                'category': 'status_mismatch',
                'category_label': 'تضارب حالة الكرت',
                'severity': 'danger',
                'details': f"حالة الكرت في النظام ({row['status']}) بينما كلمة مروره لا تزال نشطة في radcheck.",
                'suggested_fix': 'إلغاء وتطهير سجل الدخول من FreeRADIUS',
                'action_code': 'revoke_auth'
            })
            
        audit_results['status_mismatch_count'] = len(mismatches)
        
        # -------------------------------------------------------------
        # 4. Stale Zombie Accounting Sessions (> 10m no heartbeat)
        # -------------------------------------------------------------
        cutoff_str = get_heartbeat_cutoff(10)
        
        stale_acct = query_all("""
            SELECT radacctid, username, acctstarttime, acctupdatetime, nasipaddress, framedipaddress
            FROM radacct
            WHERE acctstoptime IS NULL
              AND (acctupdatetime < ? OR (acctupdatetime IS NULL AND acctstarttime < ?))
            LIMIT 200
        """, (cutoff_str, cutoff_str))
        for row in stale_acct:
            audit_results['issues'].append({
                'id': f"stale_acct_{row['radacctid']}",
                'username': row['username'],
                'category': 'stale_sessions',
                'category_label': 'جلسة معلقة (Zombie Session)',
                'severity': 'warning',
                'details': f"جلسة برقم #{row['radacctid']} مفتوحة بدون تحديث حي منذ أكثر من 10 دقائق (NAS: {row['nasipaddress'] or '-'}).",
                'suggested_fix': 'إغلاق الجلسة وتحديث وقت الانتهاء',
                'action_code': 'close_stale_session',
                'session_id': row['radacctid']
            })
            
        audit_results['stale_sessions_count'] = len(stale_acct)

        # -------------------------------------------------------------
        # 5. Invalid Check Attributes in radcheck (Causes Login Rejection)
        # -------------------------------------------------------------
        invalid_attrs = query_all("""
            SELECT id, username, attribute, value, op
            FROM radcheck
            WHERE attribute NOT IN ('Cleartext-Password', 'Calling-Station-Id', 'Auth-Type', 'Expiration')
            LIMIT 200
        """)
        for row in invalid_attrs:
            audit_results['issues'].append({
                'id': f"invalid_attr_{row['id']}",
                'username': row['username'],
                'category': 'invalid_check_attribute',
                'category_label': 'سمة مصادقة خاطئة تسبب الرفض',
                'severity': 'danger',
                'details': f"يوجد حقل ({row['attribute']} = {row['value']}) في جدول radcheck مما يمنع FreeRADIUS من المصادقة ويظهر خطأ في اسم المستخدم وكلمة المرور.",
                'suggested_fix': f"حذف سمة {row['attribute']} الخاطئة من radcheck",
                'action_code': 'purge_invalid_attr',
                'record_id': row['id'],
                'attribute_name': row['attribute']
            })
            
        audit_results['invalid_attrs_count'] = len(invalid_attrs)
        
        # Total counts
        audit_results['total_issues'] = len(audit_results['issues'])
        audit_results['duration_seconds'] = round(time.time() - start_t, 2)
        
    except Exception as e:
        print(f"[ERROR] run_database_audit: {str(e)}")
        
    return audit_results


def fix_audit_issue(action_code, username, extra_data=None):
    """
    Resolves a specific audit issue based on action_code.
    """
    if not username:
        return {'success': False, 'message': 'اسم المستخدم مطلوب'}
        
    extra_data = extra_data or {}
    
    try:
        if action_code == 'purge_orphan':
            execute_update('DELETE FROM radcheck WHERE username = ?', (username,))
            execute_update('DELETE FROM radreply WHERE username = ?', (username,))
            execute_update('DELETE FROM radusergroup WHERE username = ?', (username,))
            return {'success': True, 'message': f"تم حذف السجلات اليتيمة للمستخدم [{username}] من FreeRADIUS بنجاح."}
            
        elif action_code == 'sync_auth_limits':
            v = query_one("""
                SELECT v.*, p.name as package_name 
                FROM wisp_vouchers v
                JOIN wisp_packages p ON v.package_id = p.id
                WHERE v.username = ?
            """, (username,))
            if not v:
                return {'success': False, 'message': f"لم يتم العثور على الكرت [{username}] في جدول الكروت."}
                
            # Upsert radcheck
            execute_update('DELETE FROM radcheck WHERE username = ? AND attribute = "Cleartext-Password"', (username,))
            execute_write("""
                INSERT INTO radcheck (username, attribute, op, value)
                VALUES (?, 'Cleartext-Password', ':=', ?)
            """, (username, v['password'] or username))
            
            # Upsert radusergroup
            execute_update('DELETE FROM radusergroup WHERE username = ?', (username,))
            execute_write("""
                INSERT INTO radusergroup (username, groupname, priority)
                VALUES (?, ?, 1)
            """, (username, v['package_name']))
            
            return {'success': True, 'message': f"تمت إعادة مزامنة بيانات المصادقة والباقة للكرت [{username}] بنجاح."}
            
        elif action_code == 'revoke_auth':
            execute_update('DELETE FROM radcheck WHERE username = ?', (username,))
            execute_update('DELETE FROM radusergroup WHERE username = ?', (username,))
            execute_update('DELETE FROM radreply WHERE username = ?', (username,))
            return {'success': True, 'message': f"تم تطهير وإلغاء صلاحيات الدخول للمستخدم المعطل [{username}] بنجاح."}
            
        elif action_code == 'close_stale_session':
            session_id = extra_data.get('session_id')
            if session_id:
                execute_update("""
                    UPDATE radacct 
                    SET acctstoptime = COALESCE(acctupdatetime, CURRENT_TIMESTAMP),
                        acctterminatecause = 'Admin-Reset-Stale'
                    WHERE radacctid = ? AND acctstoptime IS NULL
                """, (int(session_id),))
            else:
                execute_update("""
                    UPDATE radacct 
                    SET acctstoptime = COALESCE(acctupdatetime, CURRENT_TIMESTAMP),
                        acctterminatecause = 'Admin-Reset-Stale'
                    WHERE username = ? AND acctstoptime IS NULL
                """, (username,))
            return {'success': True, 'message': f"تم إغلاق الجلسة المعلقة للمستخدم [{username}] بنجاح."}
            
        elif action_code == 'purge_invalid_attr':
            rec_id = extra_data.get('record_id')
            attr_name = extra_data.get('attribute_name')
            if rec_id:
                execute_write("DELETE FROM radcheck WHERE id = ?", (int(rec_id),))
            elif attr_name:
                execute_write("DELETE FROM radcheck WHERE username = ? AND attribute = ?", (username, attr_name))
            else:
                execute_write("DELETE FROM radcheck WHERE username = ? AND attribute NOT IN ('Cleartext-Password', 'Calling-Station-Id', 'Auth-Type', 'Expiration')", (username,))
            return {'success': True, 'message': f"تم حذف السمة الخاطئة للمستخدم [{username}] من FreeRADIUS بنجاح."}

        else:
            return {'success': False, 'message': f"نوع الإجراء غير معروف: {action_code}"}
            
    except Exception as e:
        return {'success': False, 'message': f"خطأ أثناء الإصلاح: {str(e)}"}


def fix_all_audit_issues():
    """
    High-Performance Bulk Engineering Fix:
    1. Purges lingering radcheck, radusergroup, radreply entries for expired/disabled/recharged vouchers and subscribers.
    2. Purges orphaned FreeRADIUS records not linked to any active voucher or subscriber.
    3. Purges invalid check attributes from radcheck (such as misplaced Simultaneous-Use).
    4. Closes stale zombie sessions older than 10m.
    5. Synchronizes missing authentication credentials for active/unused vouchers.
    """
    start_t = time.time()
    total_fixed = 0
    
    try:
        # 1. Purge radcheck for expired/recharged/disabled vouchers
        del_v_rc = execute_update("""
            DELETE FROM radcheck 
            WHERE username IN (
                SELECT username FROM wisp_vouchers 
                WHERE status IN ('expired', 'recharged', 'disabled')
            )
        """) or 0
        total_fixed += del_v_rc
        
        # Purge radusergroup for expired vouchers
        execute_update("""
            DELETE FROM radusergroup 
            WHERE username IN (
                SELECT username FROM wisp_vouchers 
                WHERE status IN ('expired', 'recharged', 'disabled')
            )
        """)
        
        # Purge radreply for expired vouchers
        execute_update("""
            DELETE FROM radreply 
            WHERE username IN (
                SELECT username FROM wisp_vouchers 
                WHERE status IN ('expired', 'recharged', 'disabled')
            )
        """)

        # 2. Purge radcheck for expired subscribers
        del_s_rc = execute_update("""
            DELETE FROM radcheck 
            WHERE username IN (
                SELECT username FROM wisp_subscribers 
                WHERE status IN ('expired', 'disabled', 'suspended')
            )
        """) or 0
        total_fixed += del_s_rc

        # 3. Purge orphaned FreeRADIUS records
        del_orphans = execute_update("""
            DELETE FROM radcheck 
            WHERE username NOT IN ('healthcheck', 'probe_user', 'admin')
              AND username NOT IN (SELECT username FROM wisp_vouchers)
              AND username NOT IN (SELECT username FROM wisp_subscribers)
        """) or 0
        total_fixed += del_orphans

        execute_update("""
            DELETE FROM radusergroup 
            WHERE username NOT IN ('healthcheck', 'probe_user', 'admin')
              AND username NOT IN (SELECT username FROM wisp_vouchers)
              AND username NOT IN (SELECT username FROM wisp_subscribers)
        """)

        # 4. Purge invalid check attributes
        del_invalid = execute_update("""
            DELETE FROM radcheck 
            WHERE attribute NOT IN ('Cleartext-Password', 'Calling-Station-Id', 'Auth-Type', 'Expiration')
        """) or 0
        total_fixed += del_invalid

        # 5. Close stale zombie sessions
        cutoff_str = get_heartbeat_cutoff(10)
        closed_zombies = execute_update("""
            UPDATE radacct 
            SET acctstoptime = COALESCE(acctupdatetime, CURRENT_TIMESTAMP),
                acctterminatecause = 'Admin-Reset-Stale'
            WHERE acctstoptime IS NULL 
              AND (acctupdatetime < ? OR (acctupdatetime IS NULL AND acctstarttime < ?))
        """, (cutoff_str, cutoff_str)) or 0
        total_fixed += closed_zombies

        # 6. Resync missing auth credentials for active/unused/used vouchers
        missing_vouchers = query_all("""
            SELECT v.username, v.password, p.name as package_name
            FROM wisp_vouchers v
            JOIN wisp_packages p ON v.package_id = p.id
            LEFT JOIN radcheck rc ON v.username = rc.username AND rc.attribute = 'Cleartext-Password'
            WHERE v.status IN ('active', 'used', 'unused')
              AND (v.expires_at IS NULL OR v.expires_at > CURRENT_TIMESTAMP)
              AND rc.id IS NULL
            LIMIT 500
        """)
        for mv in missing_vouchers:
            execute_write("INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)", (mv['username'], mv['password'] or mv['username']))
            total_fixed += 1

        elapsed = round(time.time() - start_t, 2)
        return {
            'success': True,
            'fixed_count': total_fixed,
            'errors_count': 0,
            'duration_seconds': elapsed,
            'message': f"تم بنجاح تطهير ومعالجة {total_fixed:,} سجلاً ومطابقة حالة قاعدة البيانات بالكامل خلال {elapsed} ثانية."
        }
    except Exception as e:
        elapsed = round(time.time() - start_t, 2)
        return {
            'success': False,
            'fixed_count': total_fixed,
            'errors_count': 1,
            'duration_seconds': elapsed,
            'message': f"حدث خطأ أثناء المعالجة: {str(e)}"
        }


def factory_reset_database(keep_packages=True, keep_resellers=False, admin_user='admin'):
    """
    Complete Factory Reset / Wipe of all operational data:
    - Vouchers, Batches, Sales, Invoices
    - Subscribers
    - FreeRADIUS tables: radcheck, radreply, radusergroup, radgroupcheck, radgroupreply, radacct, radpostauth
    - Reseller transactions and wallet balances
    - Global sequence tables
    - Optionally wipes packages and resellers (keeping primary admin).
    - Resets AUTO_INCREMENT counters on all operational tables.
    """
    from database.db import get_connection, is_mysql_conn, log_audit
    db = get_connection()
    cur = db.cursor()
    try:
        if is_mysql_conn(db):
            cur.execute("SET foreign_key_checks = 0;")
            cur.execute("SET unique_checks = 0;")

        # 1. Clear FreeRADIUS Tables
        cur.execute("DELETE FROM radcheck;")
        cur.execute("DELETE FROM radreply;")
        cur.execute("DELETE FROM radusergroup;")
        cur.execute("DELETE FROM radgroupcheck;")
        cur.execute("DELETE FROM radgroupreply;")
        cur.execute("DELETE FROM radacct;")
        cur.execute("DELETE FROM radpostauth;")

        # 2. Clear WISP Application Tables
        cur.execute("DELETE FROM wisp_voucher_sales;")
        cur.execute("DELETE FROM wisp_vouchers;")
        cur.execute("DELETE FROM wisp_voucher_batches;")
        cur.execute("DELETE FROM wisp_subscribers;")
        cur.execute("DELETE FROM wisp_invoices;")
        cur.execute("DELETE FROM wisp_reseller_transactions;")
        cur.execute("DELETE FROM wisp_global_sequence;")

        if not keep_resellers:
            # Keep primary admin account
            cur.execute("DELETE FROM wisp_managers WHERE id > 1 AND LOWER(username) NOT IN ('admin', 'super_admin', 'root');")
            cur.execute("UPDATE wisp_managers SET wallet_balance = 0.00 WHERE id = 1 OR LOWER(username) IN ('admin', 'super_admin', 'root');")
        else:
            cur.execute("UPDATE wisp_managers SET wallet_balance = 0.00;")

        if not keep_packages:
            cur.execute("DELETE FROM wisp_packages;")

        # 3. Reset AUTO_INCREMENT on wiped tables
        tables_to_reset = [
            'wisp_subscribers', 'wisp_vouchers', 'wisp_voucher_batches',
            'wisp_voucher_sales', 'wisp_invoices', 'wisp_reseller_transactions',
            'wisp_global_sequence', 'radacct', 'radpostauth'
        ]
        if not keep_packages:
            tables_to_reset.append('wisp_packages')
        if not keep_resellers:
            tables_to_reset.append('wisp_managers')

        if is_mysql_conn(db):
            for tbl in tables_to_reset:
                try:
                    cur.execute(f"ALTER TABLE {tbl} AUTO_INCREMENT = 1;")
                except Exception:
                    pass
            cur.execute("SET foreign_key_checks = 1;")
            cur.execute("SET unique_checks = 1;")

        db.commit()
        log_audit(1, admin_user or 'admin', 'FACTORY_RESET', 'system', 'Complete database wipe and factory reset executed.', '127.0.0.1')
        
        return {
            'success': True,
            'message': 'تم تصفير وإعادة ضبط قاعدة البيانات بالكامل بنجاح. عادت القاعدة جديدة ونظيفة 100%.'
        }
    except Exception as e:
        db.rollback()
        if is_mysql_conn(db):
            try:
                cur.execute("SET foreign_key_checks = 1;")
                cur.execute("SET unique_checks = 1;")
                db.commit()
            except Exception:
                pass
        return {
            'success': False,
            'message': f"فشل تصفير قاعدة البيانات: {str(e)}"
        }
    finally:
        cur.close()
        db.close()

