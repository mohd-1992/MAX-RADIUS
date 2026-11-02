"""Explicit, receipted subscriber edits. Package changes keep their existing cycle path."""
from contextlib import nullcontext
import datetime
import hashlib
import ipaddress
import json
import re

from database.db import db_session, query_one, adapt_query, is_mysql_conn

PERSONAL = ('password','full_name','phone','national_id','address','mac_binding','static_ip','notes')
REVISION_FIELDS = ('id','username','password','full_name','phone','national_id','address','notes',
                   'service_type','package_id','status','expires_at','last_renewed_at','first_used_at',
                   'mac_binding','static_ip','snap_volume_quota_mb','extra_quota_mb')


def revision(row):
    return hashlib.sha256(json.dumps({k:str(row.get(k) or '') for k in REVISION_FIELDS},sort_keys=True).encode()).hexdigest()


def fingerprint(kind, subscriber_id, actor, payload):
    return hashlib.sha256(json.dumps([kind,int(subscriber_id),actor,payload],sort_keys=True,default=str).encode()).hexdigest()


def receipt(operation_id, subscriber_id, actor, digest, conn=None):
    if not re.fullmatch(r'[a-f0-9]{32}', str(operation_id or '')):
        raise ValueError('طلب التعديل غير صالح؛ أعد فتح صفحة المشترك.')
    if conn:
        with conn.cursor() as cur:
            cur.execute(adapt_query('SELECT * FROM wisp_subscriber_edit_receipts WHERE operation_id=?',conn),(operation_id,))
            row=cur.fetchone()
    else:
        row=query_one('SELECT * FROM wisp_subscriber_edit_receipts WHERE operation_id=?',(operation_id,))
    if row:
        if int(row['subscriber_id'])!=int(subscriber_id) or row['actor']!=actor or row['fingerprint']!=digest:
            raise ValueError('هذا الطلب استُخدم لتعديل مختلف؛ أعد فتح الصفحة.')
        return json.loads(row['result'])


def save_receipt(conn, operation_id, subscriber_id, actor, digest, result):
    with conn.cursor() as cur:
        cur.execute(adapt_query('INSERT INTO wisp_subscriber_edit_receipts(operation_id,subscriber_id,actor,fingerprint,result) VALUES(?,?,?,?,?)',conn),
                    (operation_id,subscriber_id,actor,digest,json.dumps(result,ensure_ascii=False)))


def parse_expiry(value):
    if value is None or str(value).strip()=='':
        return None
    try:
        dt=datetime.datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is not None or dt.year<2000 or dt.year>2100:
            raise ValueError()
        return dt.replace(microsecond=0)
    except (ValueError,TypeError):
        raise ValueError('حدد تاريخًا ووقتًا صحيحين حسب توقيت النظام.') from None


def validate(row, data):
    username=str(data.get('username',row['username'])).strip()
    if username!=row['username'] and not re.fullmatch(r'[A-Za-z0-9_.@+\-]{1,64}',username):
        raise ValueError('اسم المستخدم: من 1 إلى 64 حرفًا إنجليزيًا أو رقمًا أو . _ @ + - دون مسافات.')
    service=data.get('service_type',row['service_type'])
    if service not in ('hotspot','pppoe'):
        raise ValueError('اختر Hotspot أو PPPoE.')
    mac=str(data.get('mac_binding',row.get('mac_binding')) or '').strip().upper().replace('-',':')
    from services.account_mac_service import reject_legacy_mac_change
    reject_legacy_mac_change('subscriber', row, mac)
    if mac and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',mac):
        raise ValueError('عنوان MAC غير صالح.')
    ip=str(data.get('static_ip',row.get('static_ip')) or '').strip()
    if ip:
        try:
            ip=str(ipaddress.IPv4Address(ip))
        except ValueError:
            raise ValueError('عنوان IPv4 الثابت غير صالح.') from None
    expiry=parse_expiry(data.get('expires_at',row.get('expires_at')))
    if 'package_id' in data and int(data['package_id'])!=int(row['package_id']):
        raise ValueError('استخدم قسم تغيير الباقة داخل التبويب لبدء الدورة الجديدة وخيار الترحيل.')
    personal={k:data.get(k,row.get(k) or '') for k in PERSONAL}
    for key in PERSONAL:
        if key != 'notes':
            personal[key] = str(personal[key] or '').strip()
    personal.update(mac_binding=mac,static_ip=ip)
    if not str(personal['password']).strip():
        raise ValueError('كلمة المرور مطلوبة.')
    return dict(username=username,service_type=service,expires_at=expiry,personal=personal)


def _available(conn, subscriber_id, old_username, new_username):
    with conn.cursor() as cur:
        cur.execute('UPDATE wisp_username_write_lock SET id=id WHERE id=1')
        cur.execute(adapt_query('SELECT id FROM wisp_subscribers WHERE LOWER(username)=LOWER(?) AND id<>? UNION ALL SELECT id FROM wisp_vouchers WHERE LOWER(username)=LOWER(?) LIMIT 1',conn),
                    (new_username,subscriber_id,new_username))
        if cur.fetchone():raise ValueError('اسم المستخدم مستخدم في حساب أو كرت آخر.')
        cur.execute(adapt_query('SELECT voucher_id FROM wisp_voucher_names WHERE LOWER(username)=LOWER(?) LIMIT 1',conn),(new_username,))
        if cur.fetchone():raise ValueError('الاسم مرتبط بتاريخ كرت آخر.')
        for name in (old_username,new_username):
            cur.execute(adapt_query('SELECT subscriber_id FROM wisp_subscriber_names WHERE LOWER(username)=LOWER(?)',conn),(name,))
            owner=cur.fetchone()
            if owner and int(owner['subscriber_id'])!=int(subscriber_id):
                raise ValueError('الاسم مرتبط بتاريخ مشترك آخر ولا يمكن نقله لهذا الحساب.')
        if new_username.casefold()!=old_username.casefold():
            for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
                cur.execute(adapt_query(f'SELECT username FROM {table} WHERE LOWER(username)=LOWER(?) AND (account_subscriber_id IS NULL OR account_subscriber_id<>?) LIMIT 1',conn),(new_username,subscriber_id))
                if cur.fetchone():raise ValueError('الاسم الجديد له سجلات اتصال غير مرتبطة بهذا المشترك؛ اختر اسمًا آخر.')
            cur.execute(adapt_query('SELECT id FROM wisp_loyalty_wallets WHERE LOWER(username)=LOWER(?)',conn),(new_username,))
            if cur.fetchone():raise ValueError('الاسم الجديد مرتبط بمحفظة نقاط أخرى؛ اختر اسمًا آخر.')


def _claim_history(conn, subscriber_id, old, new):
    with conn.cursor() as cur:
        for name in (old,new):
            cur.execute(adapt_query('INSERT INTO wisp_subscriber_names(username,subscriber_id) VALUES(?,?) ON DUPLICATE KEY UPDATE subscriber_id=VALUES(subscriber_id)',conn),(name,subscriber_id))
        for table in ('radacct','radacct_archive','wisp_imported_session_history','wisp_traffic_history'):
            cur.execute(adapt_query(f'''UPDATE {table} SET account_subscriber_id=?,account_username=?
                WHERE account_subscriber_id=? OR (LOWER(username)=LOWER(?) AND account_subscriber_id IS NULL)''',conn),
                        (subscriber_id,new,subscriber_id,old))
        for table in ('radcheck','radreply','radusergroup'):
            cur.execute(adapt_query(f'DELETE FROM {table} WHERE LOWER(username)=LOWER(?)',conn),(old,))
        # The live points wallet follows its account; immutable transaction logs
        # retain the identity used when the points were earned or redeemed.
        cur.execute(adapt_query('UPDATE wisp_loyalty_wallets SET username=? WHERE LOWER(username)=LOWER(?)',conn),(new,old))


def edit_subscriber(subscriber_id, data, actor='admin', can_change_expiry=False):
    operation_id=str(data.get('operation_id') or '')
    payload={k:str(v) for k,v in dict(data).items() if k not in ('csrf_token','return_to_details')}
    digest=fingerprint('profile',subscriber_id,actor,payload)
    previous=receipt(operation_id,subscriber_id,actor,digest)
    if previous:return previous
    row=query_one('SELECT * FROM wisp_subscribers WHERE id=?',(subscriber_id,))
    if not row:raise ValueError('المشترك غير موجود.')
    desired=validate(row,data)
    if data.get('expected_revision')!=revision(row):
        raise ValueError('تغيرت بيانات المشترك منذ فتح الصفحة؛ أعد فتحها قبل الحفظ.')
    old_expiry=parse_expiry(row.get('expires_at'))
    if desired['expires_at']!=old_expiry and not can_change_expiry:
        raise ValueError('تعديل تاريخ الانتهاء يحتاج صلاحية تجديد المشتركين.')
    identity_change=(desired['username']!=row['username'] or desired['service_type']!=row['service_type']
                     or desired['personal']['mac_binding']!=(row.get('mac_binding') or '').upper().replace('-',':')
                     or desired['personal']['static_ip']!=(row.get('static_ip') or ''))
    sensitive=identity_change or desired['expires_at']!=old_expiry
    # Validate collision before any NAS side effect, then repeat under write locks.
    with db_session() as conn:_available(conn,subscriber_id,row['username'],desired['username'])
    from services.renewal_settlement_service import settled_account,wait_for_final_accounting
    from services.online_policy_service import local
    with (settled_account(row['username'], identity_change=True) if identity_change else
          settled_account(row['username']) if sensitive else nullcontext()):
        previous=receipt(operation_id,subscriber_id,actor,digest)
        if previous:return previous
        if identity_change:
            from services.nas_session_settlement import freeze_and_disconnect
            session_ids = tuple(local.snapshot)
            freeze_and_disconnect(local.snapshot,include_ppp=True)
            wait_for_final_accounting(row['username'], session_ids=session_ids)
        with db_session() as conn:
            with conn.cursor() as cur:
                cur.execute(adapt_query('SELECT *,CURRENT_TIMESTAMP AS checked_at FROM wisp_subscribers WHERE id=?'+(' FOR UPDATE' if is_mysql_conn(conn) else ''),conn),(subscriber_id,))
                current=cur.fetchone()
                previous=receipt(operation_id,subscriber_id,actor,digest,conn=conn)
                if previous:return previous
                if not current or revision(current)!=data['expected_revision']:
                    raise ValueError('تغيرت بيانات المشترك أثناء العملية؛ أعد فتح الصفحة.')
                from services.subscriber_audit_service import subscriber_changes
                summary = subscriber_changes(current, desired)
                if not summary:
                    result = {'success': True, 'message': 'لا توجد تغييرات جديدة لحفظها.', 'username': current['username']}
                    save_receipt(conn,operation_id,subscriber_id,actor,digest,result)
                    return result
                _available(conn,subscriber_id,current['username'],desired['username'])
                if desired['username']!=current['username']:
                    _claim_history(conn,subscriber_id,current['username'],desired['username'])
                cur.execute(adapt_query('UPDATE wisp_subscribers SET username=?,service_type=?,expires_at=? WHERE id=?',conn),
                            (desired['username'],desired['service_type'],desired['expires_at'],subscriber_id))
                if desired['expires_at']!=old_expiry and current['status'] not in ('suspended','disabled'):
                    from services.account_lifecycle_service import expiry_reason,ACCOUNTING_UNCERTAIN_REASON
                    candidate=dict(current,username=desired['username'],service_type=desired['service_type'],expires_at=desired['expires_at'],status='active')
                    reason=expiry_reason(conn,'subscriber',candidate)
                    if reason==ACCOUNTING_UNCERTAIN_REASON:raise ValueError('تعذر تأكيد استهلاك الدورة؛ لم يحفظ تعديل الصلاحية.')
                    cur.execute(adapt_query('UPDATE wisp_subscribers SET status=? WHERE id=?',conn),('expired' if reason else 'active',subscriber_id))
            from services.subscriber_service import update_subscriber
            update_subscriber(subscriber_id,desired['personal'],admin_username=actor,conn=conn,record_audit=False)
            with conn.cursor() as cur:
                cur.execute(adapt_query("INSERT INTO user_audit_logs(user_type,user_id,username,admin_name,action,change_details) VALUES('subscriber',?,?,?,'EDIT_CONTRACT',?)",conn),
                            (subscriber_id,desired['username'],actor,json.dumps(summary,ensure_ascii=False)))
                cur.execute(adapt_query("INSERT INTO wisp_audit_logs(admin_id,username,action,module,details) VALUES(1,?,'EDIT_CONTRACT','subscribers',?)",conn),
                            (actor,json.dumps({'subscriber_id':subscriber_id,'changes':summary},ensure_ascii=False)))
            result={'success':True,'message':'تم حفظ تعديل المشترك مع حفظ استهلاكه ودورته وتاريخه.','username':desired['username']}
            save_receipt(conn,operation_id,subscriber_id,actor,digest,result)
        if sensitive:
            # Renamed identity has no surviving old session; old-name outbox is skipped.
            local.operation_succeeded=desired['username']==row['username']
    return result
