"""Receipted voucher edits preserving physical session and sale history."""
from contextlib import nullcontext
import hashlib
import json
import re

from database.db import db_session, query_one, adapt_query, is_mysql_conn
from services.subscriber_edit_service import parse_expiry, fingerprint

REVISION_FIELDS = ('id','username','password','pin_code','package_id','status','expires_at',
    'last_renewed_at','first_used_at','bound_mac','balance','extra_quota_mb','loan_balance_mb',
    'snap_volume_quota_mb','snap_uptime_limit_mins','policy_frozen_at','snap_validity_value',
    'snap_validity_unit','snap_rate_download','snap_rate_upload','snap_rate_limit_str')


def revision(row):
    return hashlib.sha256(json.dumps({k:str(row.get(k) or '') for k in REVISION_FIELDS},sort_keys=True).encode()).hexdigest()


def receipt(operation_id, voucher_id, actor, digest, conn=None):
    if not re.fullmatch(r'[a-f0-9]{32}',str(operation_id or '')):
        raise ValueError('طلب تعديل الكرت غير صالح؛ أعد فتح الصفحة.')
    sql='SELECT * FROM wisp_voucher_edit_receipts WHERE operation_id=?'
    if conn:
        with conn.cursor() as cur:
            cur.execute(adapt_query(sql,conn),(operation_id,));row=cur.fetchone()
    else:row=query_one(sql,(operation_id,))
    if row:
        if int(row['voucher_id'])!=int(voucher_id) or row['actor']!=actor or row['fingerprint']!=digest:
            raise ValueError('هذا الطلب استُخدم لعملية أخرى؛ أعد فتح صفحة الكرت.')
        return json.loads(row['result'])


def save_receipt(conn,operation_id,voucher_id,actor,digest,result):
    with conn.cursor() as cur:
        cur.execute(adapt_query('INSERT INTO wisp_voucher_edit_receipts(operation_id,voucher_id,actor,fingerprint,result) VALUES(?,?,?,?,?)',conn),
                    (operation_id,voucher_id,actor,digest,json.dumps(result,ensure_ascii=False)))


def _available(conn,card_id,old,new):
    with conn.cursor() as cur:
        cur.execute('UPDATE wisp_username_write_lock SET id=id WHERE id=1')
        cur.execute(adapt_query('SELECT id FROM wisp_vouchers WHERE LOWER(username)=LOWER(?) AND id<>? UNION ALL SELECT id FROM wisp_subscribers WHERE LOWER(username)=LOWER(?) LIMIT 1',conn),(new,card_id,new))
        if cur.fetchone():raise ValueError('اسم المستخدم مستخدم لحساب أو كرت آخر.')
        for name in (old,new):
            cur.execute(adapt_query('SELECT subscriber_id FROM wisp_subscriber_names WHERE LOWER(username)=LOWER(?)',conn),(name,))
            if cur.fetchone():raise ValueError('الاسم مرتبط بتاريخ مشترك آخر.')
            cur.execute(adapt_query('SELECT voucher_id FROM wisp_voucher_names WHERE LOWER(username)=LOWER(?)',conn),(name,))
            owner=cur.fetchone()
            if owner and int(owner['voucher_id'])!=int(card_id):raise ValueError('الاسم مرتبط بتاريخ كرت آخر.')
        if new!=old:
            for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
                cur.execute(adapt_query(f'SELECT username FROM {table} WHERE LOWER(username)=LOWER(?) AND (account_voucher_id IS NULL OR account_voucher_id<>? OR account_subscriber_id IS NOT NULL) LIMIT 1',conn),(new,card_id))
                if cur.fetchone():raise ValueError('الاسم الجديد له تاريخ اتصال غير مرتبط بهذا الكرت.')
            cur.execute(adapt_query('SELECT id FROM wisp_loyalty_wallets WHERE LOWER(username)=LOWER(?)',conn),(new,))
            if cur.fetchone():raise ValueError('الاسم الجديد مرتبط بمحفظة نقاط أخرى.')


def _claim_history(conn,card_id,old,new):
    with conn.cursor() as cur:
        for name in (old,new):
            cur.execute(adapt_query('INSERT INTO wisp_voucher_names(username,voucher_id) VALUES(?,?) ON DUPLICATE KEY UPDATE voucher_id=VALUES(voucher_id)',conn),(name,card_id))
        for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
            cur.execute(adapt_query(f'''UPDATE {table} SET account_voucher_id=?,account_username=?
                WHERE account_voucher_id=? OR (LOWER(username)=LOWER(?) AND account_voucher_id IS NULL AND account_subscriber_id IS NULL)''',conn),(card_id,new,card_id,old))
        for table in ('radcheck','radreply','radusergroup'):
            cur.execute(adapt_query(f'DELETE FROM {table} WHERE LOWER(username)=LOWER(?)',conn),(old,))
        cur.execute(adapt_query('UPDATE wisp_loyalty_wallets SET username=? WHERE LOWER(username)=LOWER(?)',conn),(new,old))


def edit_voucher(card_id,data,actor='admin',can_change_expiry=False):
    op=str(data.get('operation_id') or '')
    digest=fingerprint('voucher-profile',card_id,actor,{k:str(v) for k,v in dict(data).items() if k!='csrf_token'})
    previous=receipt(op,card_id,actor,digest)
    if previous:return previous
    row=query_one('SELECT * FROM wisp_vouchers WHERE id=?',(card_id,))
    if not row:raise ValueError('الكرت غير موجود.')
    if data.get('expected_revision')!=revision(row):raise ValueError('تغيرت بيانات الكرت؛ أعد فتح الصفحة.')
    if row['status']=='recharged':raise ValueError('الكرت مستخدم للشحن ولا يمكن تعديل عقده.')
    name=str(data.get('username',row['username'])).strip()
    password=str(data.get('password',row.get('password')) or '').strip()
    mac=str(data.get('bound_mac',row.get('bound_mac')) or '').strip().upper().replace('-',':')
    from services.account_mac_service import reject_legacy_mac_change
    reject_legacy_mac_change('voucher', row, mac)
    expiry=parse_expiry(data.get('expires_at',row.get('expires_at')))
    old_expiry=parse_expiry(row.get('expires_at'))
    if name!=row['username'] and not re.fullmatch(r'[A-Za-z0-9_.@+\-]{1,64}',name):raise ValueError('اسم المستخدم غير صالح.')
    if not password or len(password)>128:raise ValueError('كلمة المرور مطلوبة وبحد أقصى 128 حرفًا.')
    if mac and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',mac):raise ValueError('عنوان MAC غير صالح.')
    if 'package_id' in data and int(data['package_id'])!=int(row['package_id']):raise ValueError('استخدم قسم تغيير الباقة لبدء دورة جديدة.')
    if 'status' in data and data['status']!=row['status']:raise ValueError('تغيير الحالة من العمليات المخصصة.')
    if expiry!=old_expiry:
        if not can_change_expiry:raise ValueError('تعديل الصلاحية يحتاج إذن تمديد الكرت.')
        if row['status']=='unused':raise ValueError('صلاحية الكرت غير المستخدم تبدأ عند أول استخدام.')
    with db_session() as conn:_available(conn,card_id,row['username'],name)
    identity=name!=row['username'] or mac!=(row.get('bound_mac') or '').upper().replace('-',':')
    sensitive=identity or expiry!=old_expiry
    from services.renewal_settlement_service import settled_account,wait_for_final_accounting
    from services.online_policy_service import local
    with (settled_account(row['username'], identity_change=True) if identity else
          settled_account(row['username']) if sensitive else nullcontext()):
        previous=receipt(op,card_id,actor,digest)
        if previous:return previous
        if identity:
            from services.nas_session_settlement import disconnect_for_identity
            session_ids = tuple(local.snapshot)
            disconnect_for_identity(local.snapshot)
            wait_for_final_accounting(row['username'], session_ids=session_ids)
        with db_session() as conn:
            with conn.cursor() as cur:
                cur.execute(adapt_query('SELECT *,CURRENT_TIMESTAMP AS checked_at FROM wisp_vouchers WHERE id=?'+(' FOR UPDATE' if is_mysql_conn(conn) else ''),conn),(card_id,));current=cur.fetchone()
                previous=receipt(op,card_id,actor,digest,conn)
                if previous:return previous
                if not current or revision(current)!=data['expected_revision']:raise ValueError('تغيرت بيانات الكرت أثناء العملية؛ أعد فتح الصفحة.')
                changes={}
                for key,before,after in [('username',current['username'],name),('bound_mac',current.get('bound_mac') or '',mac),('expires_at',old_expiry,expiry)]:
                    if before!=after:changes[key]={'before':str(before or ''),'after':str(after or '')}
                if password!=current.get('password'):changes['password']={'changed':True}
                result={'success':True,'message':'تم حفظ تعديل الكرت مع حفظ استهلاكه وتاريخه.' if changes else 'لا توجد تغييرات جديدة لحفظها.','username':name}
                if not changes:
                    save_receipt(conn,op,card_id,actor,digest,result);return result
                _available(conn,card_id,current['username'],name)
                if name!=current['username']:_claim_history(conn,card_id,current['username'],name)
                pin=password if current.get('pin_code')==current.get('password') else current.get('pin_code')
                cur.execute(adapt_query('UPDATE wisp_vouchers SET username=?,password=?,pin_code=?,bound_mac=?,expires_at=? WHERE id=?',conn),(name,password,pin,mac or None,expiry,card_id))
                if expiry!=old_expiry and current['status'] not in ('suspended','disabled'):
                    from services.account_lifecycle_service import expiry_reason,ACCOUNTING_UNCERTAIN_REASON
                    candidate=dict(current,username=name,expires_at=expiry,status='active')
                    reason=expiry_reason(conn,'voucher',candidate)
                    if reason==ACCOUNTING_UNCERTAIN_REASON:raise ValueError('تعذر تأكيد محاسبة الدورة؛ لم يحفظ التعديل.')
                    cur.execute(adapt_query("UPDATE wisp_vouchers SET status=?,expire_reason=? WHERE id=?",conn),('expired' if reason else 'active',reason or '',card_id))
            from core.radius_sync import sync_voucher_to_radius
            if not sync_voucher_to_radius(card_id,conn=conn):raise ValueError('تعذرت مزامنة الكرت؛ لم يحفظ التعديل.')
            with conn.cursor() as cur:
                cur.execute(adapt_query("INSERT INTO user_audit_logs(user_type,user_id,username,admin_name,action,change_details) VALUES('voucher',?,?,?,'EDIT_CARD',?)",conn),(card_id,name,actor,json.dumps(changes,ensure_ascii=False)))
                cur.execute(adapt_query("INSERT INTO wisp_audit_logs(admin_id,username,action,module,details) VALUES(1,?,'EDIT_CARD','vouchers',?)",conn),(actor,json.dumps({'voucher_id':card_id,'changes':changes},ensure_ascii=False)))
            save_receipt(conn,op,card_id,actor,digest,result)
        if sensitive:local.operation_succeeded=name==row['username']
    return result
