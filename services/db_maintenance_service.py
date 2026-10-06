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

VALID_CHECK_ATTRIBUTES = frozenset(('Cleartext-Password','Calling-Station-Id','Auth-Type','Expiration','Simultaneous-Use','Max-All-Session','Max-Daily-Session','User-Password','Crypt-Password','MD5-Password','SMD5-Password','SHA-Password','SSHA-Password','NT-Password','LM-Password','CHAP-Password','NAS-IP-Address','NAS-Identifier','NAS-Port','NAS-Port-Type','Called-Station-Id','Service-Type','Framed-Protocol','Login-Time','Group','Huntgroup-Name'))

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
            
        # 6. Safe Archiver Telemetry
        try:
            from services.accounting_archiver_service import get_archiver_status
            stats['archiver'] = get_archiver_status()
        except Exception:
            stats['archiver'] = {
                'live_sessions_count': stats.get('radacct_count', 0),
                'archived_sessions_count': 0,
                'total_archived_traffic_gb': 0.0,
                'older_90d_count': 0,
                'radacct_size_mb': 0.0,
                'archive_size_mb': 0.0,
                'oldest_session': None,
                'newest_session': None
            }

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
        'radacct_archive': {
            'arabic_name': 'أرشيف سجلات المحاسبة',
            'desc': 'الجلسات التاريخية المؤرشفة والمضغوطة',
            'icon': 'fa-file-zipper',
            'color': 'teal'
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
    from services.account_lifecycle_service import run_transaction, sql, job_lock, HISTORY_ELIGIBLE
    days = max(1, int(days))
    result = dict(success=True, retention_days=days, deleted_radacct=0, deleted_radpostauth=0, deleted_audit_logs=0)
    started = time.time()
    try:
        with job_lock('history-maintenance') as acquired:
            if not acquired:
                raise ValueError('توجد عملية صيانة سجلات أخرى قيد التنفيذ.')
            while True:
                def chunk(conn):
                    rows = sql(conn, 'SELECT a.radacctid FROM radacct a WHERE '+HISTORY_ELIGIBLE+' ORDER BY a.radacctid LIMIT 250 FOR UPDATE', (days,), 'all')
                    if not rows:
                        return 0
                    ids = [row['radacctid'] for row in rows]
                    marks = ','.join(['?']*len(ids))
                    return sql(conn, f'DELETE a FROM radacct a WHERE a.radacctid IN ({marks}) AND '+HISTORY_ELIGIBLE, tuple(ids)+(days,))
                count = run_transaction(chunk)
                result['deleted_radacct'] += count
                if not count:
                    break
            for table, column, enabled, field in [('radpostauth','authdate',prune_postauth,'deleted_radpostauth'),('wisp_audit_logs','created_at',prune_audit,'deleted_audit_logs')]:
                if enabled:
                    while True:
                        count=run_transaction(lambda conn: sql(conn,f'DELETE FROM {table} WHERE {column}<NOW()-INTERVAL ? DAY LIMIT 500',(days,)))
                        result[field]+=count
                        if count<500:break
        result['message'] = f"تم حذف {result['deleted_radacct']} سجل محاسبة قديم مع حماية الدورة الحالية وخطوط الأساس."
    except Exception as exc:
        result['success']=False
        result['message']=f'تعذر إكمال التنظيف: {exc}'
    result['duration_seconds']=round(time.time()-started,2)
    return result


def optimize_database_tables(tables=None):
    from services.account_lifecycle_service import job_lock
    try:
        with job_lock('factory-reset') as acquired, job_lock('expired-card-delete') as cleanup_idle:
            if not acquired or not cleanup_idle:
                return dict(success=False,message='توجد عملية حذف أو أرشفة أو تحسين أو إعادة مصنع قيد التنفيذ؛ أعد المحاولة بعد اكتمالها.')
            return _optimize_database_tables_locked(tables)
    except Exception as exc:
        return dict(success=False,message='تعذر تحسين الجداول: '+str(exc))


def _optimize_database_tables_locked(tables=None, progress_callback=None):
    """
    Runs OPTIMIZE TABLE on database tables to rebuild InnoDB tablespaces,
    defragment index structures, and reclaim unused disk space.
    """
    start_t = time.time()
    import re
    if not tables:
        tables = [
            'radacct', 'radacct_archive', 'radpostauth', 'radcheck', 'radusergroup', 
            'wisp_vouchers', 'wisp_voucher_sales', 'wisp_subscribers', 'wisp_audit_logs'
        ]
        
    if any(not isinstance(table,str) or not re.fullmatch(r'[A-Za-z0-9_]+',table) for table in tables):
        return dict(success=False,message='قائمة جداول غير صالحة')
    before_stats = get_detailed_table_sizes()
    before_mb = before_stats.get('total_db_mb', 0.0)
    
    optimized_results = []
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        for table_index, tbl in enumerate(tables):
            if progress_callback:
                progress_callback(table_index, len(tables), tbl)
            t0 = time.time()
            try:
                # OPTIMIZE TABLE
                cursor.execute(f"OPTIMIZE TABLE {tbl}")
                res = cursor.fetchall()
                if any(str(row.get('Msg_type','')).lower()=='error' for row in res):
                    raise RuntimeError('; '.join(str(row.get('Msg_text','')) for row in res))
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
            'success': all(item['status']=='OK' for item in optimized_results),
            'before_mb': before_mb,
            'after_mb': after_mb,
            'freed_mb': freed_mb,
            'duration_seconds': elapsed,
            'details': optimized_results,
            'message': f"انتهت معالجة {len(tables)} جداول؛ راجع نتيجة كل جدول خلال {elapsed} ثانية. (الحجم الحالي: {after_mb} MB)."
        }
        
    except Exception as e:
        return {
            'success': False,
            'message': f"خطأ أثناء تحسين الجداول: {str(e)}",
            'duration_seconds': round(time.time() - start_t, 2)
        }


def run_auto_maintenance_job(retention_days=90):
    result = prune_historical_logs(days=retention_days, prune_postauth=True)
    if not result['success']:
        raise RuntimeError(result['message'])
    # Compaction is explicit maintenance, never an automatic consequence of pruning.
    return True


def delete_expired_vouchers(delete_type='all', delete_acct=False, batch_limit=50000, min_age_days=0, progress_callback=None):
    from services.account_lifecycle_service import delete_account, job_lock
    result=dict(success=True,deleted_vouchers=0,deleted_radcheck=0,deleted_radusergroup=0,deleted_radreply=0,deleted_radacct=0,pending=0,skipped=0,processed=0,total=0,queue_failures=0)
    started=time.time()
    if delete_type not in ('all','time','quota'):
        return dict(result,success=False,message='نوع حذف غير صالح')
    def report():
        result['duration_seconds']=round(time.time()-started,2)
        result['message']=f"تمت معالجة {result['processed']} من {result['total']}؛ حذف {result['deleted_vouchers']} كرت، و{result['pending']} ينتظر إغلاق الجلسات، و{result['skipped']} تغيرت حالته. السجل المالي والمحاسبي محفوظ."
        if progress_callback:progress_callback(dict(result))
    try:
        with job_lock('expired-card-delete') as acquired:
            if not acquired:raise ValueError('الحذف أو إعادة المصنع قيد التنفيذ بالفعل.')
            where="status='expired'";params=[]
            if min_age_days:
                where+=' AND expires_at < NOW()-INTERVAL ? DAY';params.append(max(1,int(min_age_days)))
            if delete_type=='quota':
                where+=" AND (COALESCE(expire_reason,'') LIKE ? OR COALESCE(expire_reason,'') LIKE ? OR COALESCE(expire_reason,'') LIKE ?)";params+=['%البيانات%','%كوتا%','%quota%']
            elif delete_type=='time':
                where+=" AND COALESCE(expire_reason,'') NOT LIKE ? AND COALESCE(expire_reason,'') NOT LIKE ? AND COALESCE(expire_reason,'') NOT LIKE ?";params+=['%البيانات%','%كوتا%','%quota%']
            rows=query_all('SELECT id,last_renewed_at,expires_at,expire_reason FROM wisp_vouchers WHERE '+where+' ORDER BY id LIMIT ?',tuple(params)+(max(1,min(int(batch_limit),50000)),))
            result['total']=len(rows);report()
            for offset in range(0,len(rows),50):
                conn=get_connection()
                try:
                    for row in rows[offset:offset+50]:
                        result['current_card_id']=row['id']
                        state=delete_account('voucher',row['id'],expected_expired=True,delete_acct=delete_acct,min_age_days=min_age_days,expected_cycle=str(row.get('last_renewed_at') or ''),expected_reason=row.get('expire_reason'),connection=conn)
                        result['deleted_vouchers']+=bool(state.get('deleted'))
                        result['pending']+=bool(state.get('pending'))
                        result['skipped']+=not state.get('deleted') and not state.get('pending')
                        result['queue_failures']+=state.get('queue_failures',0)
                        for field in ('deleted_radcheck','deleted_radusergroup','deleted_radreply'):result[field]+=state.get(field,0)
                        result['processed']+=1
                finally:conn.close()
                report()
                if offset+50<len(rows):time.sleep(.02)
            if result['queue_failures']:
                result['success']=False;result['error']='تعذر إدراج بعض مهام الفصل؛ الحسابات محفوظة وتحتاج إعادة المحاولة.'
    except Exception as exc:
        result['success']=False;result['error']=str(exc)
        if result.get('current_card_id'):result['failed_card_id']=result['current_card_id']
    result.pop('current_card_id',None)
    result['duration_seconds']=round(time.time()-started,2)
    result['message']=f"تم حذف {result['deleted_vouchers']} كرت؛ {result['pending']} ينتظر إغلاق الجلسات و{result['skipped']} تغيرت حالته. السجل المالي والمحاسبي محفوظ."
    if not result['success']:result['message']+=' لم تكتمل العملية: '+result['error']
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
        'success': True,
        'truncated': False,
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
            SELECT t.id,t.username,t.status,p.name AS package_name,rc.id AS rc_id,rg.id AS rg_id
            FROM (
                SELECT id,username,status,package_id,expires_at FROM wisp_vouchers WHERE status IN ('active','used','unused')
                UNION ALL
                SELECT id,username,status,package_id,expires_at FROM wisp_subscribers WHERE status='active'
            ) t
            JOIN wisp_packages p ON t.package_id=p.id
            LEFT JOIN radcheck rc ON t.username=rc.username AND rc.attribute='Cleartext-Password'
            LEFT JOIN radusergroup rg ON t.username=rg.username
            WHERE (t.expires_at IS NULL OR t.expires_at>CURRENT_TIMESTAMP)
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
            SELECT t.id,t.username,t.status,t.expire_reason,rc.attribute,rc.value
            FROM (
                SELECT id,username,status,expire_reason FROM wisp_vouchers WHERE status IN ('expired','recharged','disabled','suspended')
                UNION ALL
                SELECT id,username,status,NULL AS expire_reason FROM wisp_subscribers WHERE status IN ('expired','disabled','suspended')
            ) t
            JOIN radcheck rc ON t.username=rc.username AND rc.attribute='Cleartext-Password'
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
        from services.autoheal_service import get_zombie_session_timeout
        cutoff_str = get_heartbeat_cutoff(get_zombie_session_timeout())
        
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
        allowed=sorted(VALID_CHECK_ATTRIBUTES)
        invalid_attrs = query_all("SELECT id,username,attribute,value,op FROM radcheck WHERE attribute NOT IN (" + ','.join('?' for _ in allowed) + ") LIMIT 200", tuple(allowed))
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
        audit_results['truncated'] = any(len(rows)>=200 for rows in (orphans,missing_auth,mismatches,stale_acct,invalid_attrs))
        audit_results['total_issues'] = len(audit_results['issues'])
        audit_results['duration_seconds'] = round(time.time() - start_t, 2)
        
    except Exception as e:
        audit_results.update(success=False,error=str(e),duration_seconds=round(time.time()-start_t,2))
        print(f"[ERROR] run_database_audit: {str(e)}")
        
    return audit_results


def fix_audit_issue(action_code, username, extra_data=None):
    from services.account_lifecycle_service import job_lock
    try:
        with job_lock('factory-reset') as idle:
            if not idle:return dict(success=False,message='إعادة المصنع أو الصيانة قيد التنفيذ؛ أعد المحاولة لاحقًا.')
            return _fix_audit_issue_locked(action_code,username,extra_data)
    except Exception as exc:
        return dict(success=False,message=str(exc))


def _fix_audit_issue_locked(action_code, username, extra_data=None):
    from services.account_lifecycle_service import run_transaction, sql, expiry_reason
    from services.autoheal_service import get_zombie_session_timeout
    extra_data=extra_data or {}
    if not username or username in SYSTEM_RESERVED_USERS:
        return dict(success=False,message='حساب غير صالح للإصلاح')
    try:
        def operation(conn):
            voucher=sql(conn,'SELECT *,CURRENT_TIMESTAMP checked_at FROM wisp_vouchers WHERE username=? FOR UPDATE',(username,),'one')
            subscriber=sql(conn,'SELECT *,CURRENT_TIMESTAMP checked_at FROM wisp_subscribers WHERE username=? FOR UPDATE',(username,),'one')
            row=voucher or subscriber
            if action_code=='close_stale_session':
                cutoff=get_heartbeat_cutoff(get_zombie_session_timeout())
                statement="UPDATE radacct SET acctstoptime=COALESCE(acctupdatetime,acctstarttime),acctterminatecause='Stale-Session-Timeout' WHERE username=? AND acctstoptime IS NULL AND COALESCE(acctupdatetime,acctstarttime)<?"
                params=(username,cutoff)
                if extra_data.get('session_id'):
                    statement+=' AND radacctid=?';params+=(int(extra_data['session_id']),)
                affected=sql(conn,statement,params)
                return dict(success=True,message=f'تم إغلاق {affected} جلسة متقادمة فقط؛ التحديثات المتأخرة تبقى قابلة للمصالحة.')
            if action_code=='purge_orphan':
                if row:raise ValueError('الحساب أصبح مرتبطًا؛ أُلغي حذف اليتيم.')
            elif action_code=='revoke_auth':
                if not row or row['status'] not in ('expired','disabled','suspended','recharged'):
                    raise ValueError('الحساب صالح أو تغيرت حالته؛ لم تحذف المصادقة.')
            elif action_code=='sync_auth_limits':
                kind='voucher' if voucher else 'subscriber'
                if voucher and subscriber:raise ValueError('اسم المستخدم مرتبط بكرت ومشترك؛ يلزم حل التعارض أولًا.')
                states=('unused','active','used') if voucher else ('active',)
                if not row or row['status'] not in states or expiry_reason(conn,kind,row):
                    raise ValueError('الحساب غير مؤهل لمزامنة الدخول.')
                pkg=sql(conn,'SELECT name FROM wisp_packages WHERE id=?',(row['package_id'],),'one')
                if not pkg:raise ValueError('الباقة غير موجودة')
                sql(conn,"DELETE FROM radcheck WHERE username=? AND (attribute='Cleartext-Password' OR (attribute='Auth-Type' AND value='Reject'))",(username,))
                sql(conn,"INSERT INTO radcheck (username,attribute,op,value) VALUES (?,'Cleartext-Password',':=',?)",(username,row['password'] or username))
                sql(conn,'DELETE FROM radusergroup WHERE username=?',(username,))
                sql(conn,'INSERT INTO radusergroup (username,groupname,priority) VALUES (?,?,1)',(username,pkg['name']))
                return dict(success=True,message='تمت المزامنة بعد التحقق من الحالة والرصيد.')
            elif action_code=='purge_invalid_attr':
                allowed=VALID_CHECK_ATTRIBUTES
                if extra_data.get('record_id'):
                    attr=sql(conn,'SELECT attribute FROM radcheck WHERE id=? AND username=? FOR UPDATE',(int(extra_data['record_id']),username),'one')
                    if not attr or attr['attribute'] in allowed:raise ValueError('سمة صحيحة أو سجل غير مطابق؛ لم يحذف.')
                    sql(conn,'DELETE FROM radcheck WHERE id=? AND username=?',(int(extra_data['record_id']),username))
                else:
                    attr=extra_data.get('attribute_name')
                    if not attr or attr in allowed:raise ValueError('يجب تحديد سمة غير صحيحة بعينها.')
                    sql(conn,'DELETE FROM radcheck WHERE username=? AND attribute=?',(username,attr))
                return dict(success=True,message='تم حذف السمة المحددة فقط.')
            else:raise ValueError('نوع إصلاح غير معروف')
            for table in ('radcheck','radreply','radusergroup'):
                sql(conn,f'DELETE FROM {table} WHERE username=?',(username,))
            return dict(success=True,message='تم الإصلاح بعد إعادة فحص حالة الحساب.')
        return run_transaction(operation)
    except Exception as exc:
        return dict(success=False,message=str(exc))


def fix_all_audit_issues():
    from services.voucher_service import check_and_update_expired_vouchers
    from services.autoheal_service import purge_stale_zombie_sessions
    from services.account_lifecycle_service import job_lock
    started=time.time()
    fixed=0;errors=[]
    try:
        with job_lock('audit-fix-all') as acquired:
            if not acquired:raise ValueError('توجد عملية إصلاح شامل قيد التنفيذ.')
            if not check_and_update_expired_vouchers():raise ValueError('فحص الانتهاء قيد التنفيذ؛ أعد المحاولة لاحقًا.')
            ok,msg=purge_stale_zombie_sessions()
            if not ok:raise RuntimeError(msg)
            audit=run_database_audit()
            if not audit.get('success'):raise RuntimeError(audit.get('error','تعذر إكمال الفحص'))
            seen=set()
            for issue in audit['issues']:
                key=(issue['action_code'],issue['username'],issue.get('record_id'),issue.get('session_id'))
                if key in seen:continue
                seen.add(key)
                result=fix_audit_issue(issue['action_code'],issue['username'],issue)
                if result['success']:fixed+=1
                else:errors.append(dict(issue_id=issue['id'],message=result.get('message','تعذر الإصلاح')))
            remaining=run_database_audit()
            if not remaining.get('success'):
                errors.append(dict(issue_id='verification',message=remaining.get('error','تعذر فحص النتيجة')))
            complete=remaining.get('success') and not remaining.get('truncated') and remaining['total_issues']==0 and not errors
            return dict(success=bool(complete),fixed_count=fixed,errors_count=len(errors),errors=errors,
                        remaining_issues=remaining.get('total_issues'),truncated=remaining.get('truncated',False),
                        duration_seconds=round(time.time()-started,2),
                        message=f'تم إصلاح {fixed} بند؛ تعثر {len(errors)}. ' + ('لم يرصد الفحص المتاح مشكلات متبقية.' if complete else 'لم تكتمل المعالجة؛ أعد الفحص وراجع البنود المتبقية.'))
    except Exception as exc:
        return dict(success=False,fixed_count=fixed,errors_count=len(errors)+1,errors=errors,
                    message=f'لم تكتمل المعالجة: {exc}',duration_seconds=round(time.time()-started,2))


FACTORY_RESET_TABLES = ('radcheck','radreply','radusergroup','radacct','radacct_archive','radpostauth','wisp_session_baselines','wisp_session_reservations','wisp_voucher_sales','wisp_vouchers','wisp_voucher_batches','wisp_subscribers','wisp_invoices','wisp_manager_invoices','wisp_reseller_transactions','wisp_global_sequence','user_audit_logs','wisp_loyalty_wallets','wisp_loyalty_transactions','wisp_automation_logs','wisp_deletion_requests','wisp_wallet_ledger','wisp_reseller_wallets','wisp_notification_logs','wisp_whatsapp_logs')


def _wipe_factory_database(keep_packages=True, keep_resellers=False):
    """Atomic wipe; called only by the reset coordinator after safe preparation."""
    from services.account_lifecycle_service import sql, run_transaction
    def wipe(conn):
        existing={row['TABLE_NAME'] for row in sql(conn,"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'",fetch='all')}
        tables=set(FACTORY_RESET_TABLES) & existing
        if not keep_packages:tables.add('wisp_packages')
        refs=sql(conn,'SELECT TABLE_NAME,REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL',fetch='all')
        ordered=[];visiting=set()
        def visit(table):
            if table in ordered:return
            if table in visiting:raise ValueError('توجد دورة مفاتيح خارجية تتطلب ترحيلًا قبل إعادة المصنع.')
            visiting.add(table)
            for ref in refs:
                if ref['REFERENCED_TABLE_NAME']==table and ref['TABLE_NAME'] in tables:visit(ref['TABLE_NAME'])
            visiting.remove(table);ordered.append(table)
        for table in sorted(tables):visit(table)
        for table in ordered:
            if table in ('radcheck','radreply','radusergroup'):
                sql(conn,f"DELETE FROM `{table}` WHERE username NOT IN ('healthcheck','probe_user')")
            else:
                sql(conn,f'DELETE FROM `{table}`')
        sql(conn,'UPDATE wisp_managers SET wallet_balance=0')
        if 'wisp_resellers' in existing:sql(conn,'UPDATE wisp_resellers SET balance=0')
        if not keep_resellers:
            sql(conn,"DELETE FROM wisp_managers WHERE id>1 AND LOWER(username) NOT IN ('admin','super_admin','root')")
            if 'wisp_resellers' in existing:sql(conn,'DELETE FROM wisp_resellers')
        sql(conn,'DELETE FROM radgroupreply');sql(conn,'DELETE FROM radgroupcheck')
        if keep_packages:
            from core.radius_sync import sync_package_to_radius
            for row in sql(conn,'SELECT id FROM wisp_packages ORDER BY id',fetch='all'):sync_package_to_radius(row['id'],conn=conn)
        # Retain monotonic IDs, constraints, license, NAS and the rollback archive.
        return len(ordered)
    return run_transaction(wipe)

def _reclaim_factory_space(keep_packages=True, keep_resellers=False, progress_callback=None):
    # The coordinator already owns factory-reset and cleanup locks.
    existing={row['TABLE_NAME'] for row in query_all("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'")}
    tables=set(FACTORY_RESET_TABLES) | {'radgroupcheck', 'radgroupreply'}
    if not keep_packages:tables.add('wisp_packages')
    if not keep_resellers:tables.update(('wisp_managers','wisp_resellers'))
    return _optimize_database_tables_locked(sorted(tables & existing), progress_callback)


def factory_reset_database(keep_packages=True, keep_resellers=False, admin_user="admin"):
    from services.factory_reset_service import start_factory_reset
    job, created = start_factory_reset(keep_packages, keep_resellers, admin_user)
    return dict(success=True, job=job, created=created)
