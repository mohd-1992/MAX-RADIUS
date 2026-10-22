"""Quick cards share batch issuance, ownership, wallet and RADIUS transaction."""
from core.license_feature_policy import guarded as _license_guarded
from core.license_feature_policy import require as _require_feature
import json
import re
from decimal import Decimal
from database.db import db_session, is_mysql_conn, query_one as raw_one
from core.ownership import owner_id, generation_owner, scoped_query_all, scoped_query_one
from core.time_service import get_db_storage_now
from services.voucher_service import _VoucherWriter, generate_voucher_batch

POLICY_KEY = 'reseller_card_issue_policy'
DEFAULT_POLICY = dict(prefix='', char_type='numbers', total_length=8,
                      format_type='user_pass', password_length=8)


def validate_issue_policy(data):
    policy = {key: data.get(key, value) for key, value in DEFAULT_POLICY.items()}
    policy['prefix'] = str(policy['prefix']).strip()
    if not re.fullmatch(r'[A-Za-z0-9]{0,12}', policy['prefix']):
        raise ValueError('البادئة تقبل أرقامًا وحروفًا إنجليزية فقط، بحد أقصى 12 رمزًا.')
    for field in ('total_length', 'password_length'):
        text = str(policy[field])
        if not text.isascii() or not text.isdigit() or not 4 <= int(text) <= 32:
            raise ValueError('طول اسم المستخدم وكلمة المرور يجب أن يكون بين 4 و32.')
        policy[field] = int(text)
    if policy['total_length'] - len(policy['prefix']) < 4:
        raise ValueError('اترك أربعة رموز عشوائية على الأقل بعد البادئة.')
    if policy['char_type'] not in ('numbers', 'alphanumeric'):
        raise ValueError('اختر أرقامًا فقط أو أرقامًا وحروفًا.')
    if policy['char_type'] == 'numbers' and policy['prefix'] and not policy['prefix'].isdigit():
        raise ValueError('البادئة يجب أن تكون رقمية عند اختيار أرقام فقط.')
    if policy['format_type'] not in ('pin_only', 'same_user_pass', 'user_pass'):
        raise ValueError('طريقة الدخول غير صالحة.')
    return policy


def get_issue_policy(tx=None):
    row = (tx.one if tx else raw_one)(
        'SELECT `value` FROM wisp_system_settings WHERE `key`=?', (POLICY_KEY,))
    if not row:
        return dict(DEFAULT_POLICY)
    try:
        return validate_issue_policy(json.loads(row['value']))
    except (TypeError, ValueError, AttributeError):
        raise ValueError('إعدادات إصدار كروت الموزعين غير صالحة؛ راجع الإدارة.')


@_license_guarded('quick_cards')
def save_issue_policy(data):
    if owner_id() is not None:
        raise PermissionError('إعدادات الإصدار متاحة للإدارة فقط.')
    policy = validate_issue_policy(data)
    with db_session() as conn:
        tx = _VoucherWriter(conn)
        lock = ' FOR UPDATE' if is_mysql_conn(conn) else ''
        # Also serialize the first insert when the setting does not yet exist.
        tx.one('SELECT id FROM wisp_roles WHERE id=1' + lock)
        row = tx.one('SELECT `value` FROM wisp_system_settings WHERE `key`=?', (POLICY_KEY,))
        payload = json.dumps(policy)
        if row:
            tx.write('UPDATE wisp_system_settings SET `value`=? WHERE `key`=?', (payload, POLICY_KEY))
        else:
            tx.write('INSERT INTO wisp_system_settings (`key`,`value`) VALUES (?,?)', (POLICY_KEY, payload))
    return policy


def issue_quick_cards(actor, package_id, count, request_key, reseller_id=None):
    def positive(value):
        text = str(value)
        if not text.isascii() or not text.isdigit() or int(text) < 1:
            raise ValueError('معرف الباقة والعدد غير صالحين.')
        return int(text)
    package_id, count = positive(package_id), positive(count)
    if count > 50:
        raise ValueError('الإصدار السريع يسمح من كرت واحد إلى 50 كرتًا.')
    if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', request_key):
        raise ValueError('مرجع الإصدار غير صالح؛ أعد فتح الصفحة.')
    actual_owner = generation_owner(reseller_id, package_id)
    if actual_owner is not None:
        actual_owner = positive(actual_owner)
    with db_session() as conn:
        tx = _VoucherWriter(conn)
        lock = ' FOR UPDATE' if is_mysql_conn(conn) else ''
        # Same lock order as ordinary issuance: package, then wallet.
        package = tx.one('SELECT * FROM wisp_packages WHERE id=?' + lock, (package_id,))
        if not package:
            raise ValueError('اختر باقة كروت نشطة.')
        current = tx.one('SELECT id,is_active,is_deleted FROM wisp_managers WHERE id=?' + lock, (actor['id'],))
        if not current or not current.get('is_active') or current.get('is_deleted'):
            raise ValueError('حساب الإصدار غير متاح.')
        previous = tx.one('SELECT * FROM wisp_quick_card_issues WHERE actor_id=? AND request_key=?',
                          (actor['id'], request_key))
        if previous:
            if (previous['package_id'], previous['card_count'], previous['reseller_id']) != (package_id, count, actual_owner):
                raise ValueError('مرجع الإصدار مستخدم لعملية أخرى؛ حدّث الصفحة قبل إصدار جديد.')
            return dict(previous, repeated=True)
        _require_feature('quick_cards')
        if not package.get('is_active') or package.get('service_type') not in ('hotspot', 'both'):
            raise ValueError('اختر باقة كروت نشطة.')
        policy = get_issue_policy(tx)
        batch_id, _ = generate_voucher_batch(
            name='',
            package_id=package_id, count=count, prefix=policy['prefix'],
            code_length=policy['total_length']-len(policy['prefix']), char_type=policy['char_type'],
            pin_only=policy['format_type']=='pin_only', same_user_pass=policy['format_type']=='same_user_pass',
            pass_length=policy['password_length'], reseller_id=actual_owner,
            created_by=actor['username'], _conn=conn)
        wallet = tx.one('SELECT wallet_balance FROM wisp_managers WHERE id=?', (actual_owner,)) if actual_owner else None
        amount = (Decimal(str(package['cost'] if package.get('cost') is not None else Decimal(str(package['price']))*Decimal('.8')))*count).quantize(Decimal('.01')) if actual_owner else Decimal(0)
        ident = tx.write('''INSERT INTO wisp_quick_card_issues
            (actor_id,reseller_id,request_key,package_id,card_count,batch_id,amount,balance_after,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (actor['id'], actual_owner, request_key, package_id, count,
            batch_id, str(amount), str(wallet['wallet_balance']) if wallet else None,
            get_db_storage_now().replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')))
        return dict(tx.one('SELECT * FROM wisp_quick_card_issues WHERE id=?', (ident,)), repeated=False)


def quick_issue_history(search='', package_id=None, date_from='', date_to='', reseller_id=None, page=1):
    where, params = ['1=1'], []
    if search:
        where.append('(CAST(i.id AS CHAR)=? OR EXISTS (SELECT 1 FROM wisp_vouchers v WHERE v.batch_id=i.batch_id AND v.username LIKE ?))')
        params.extend((search, '%'+search+'%'))
    for column, value in (('i.package_id', package_id), ('i.reseller_id', reseller_id)):
        if value:
            where.append(column+'=?'); params.append(int(value))
    for op, value in (('>=', date_from), ('<=', date_to)):
        if value:
            import datetime
            date = datetime.date.fromisoformat(value)
            where.append('i.created_at '+op+' ?')
            params.append(str(date)+(' 00:00:00' if op=='>=' else ' 23:59:59'))
    clause=' AND '.join(where)
    total = scoped_query_one('SELECT COUNT(*) n FROM wisp_quick_card_issues i WHERE '+clause, tuple(params))['n']
    rows = scoped_query_all('''SELECT i.*,p.name package_name,b.batch_number,m.username owner_name,
        (SELECT COUNT(*) FROM wisp_vouchers v WHERE v.batch_id=i.batch_id) available_cards
        FROM wisp_quick_card_issues i LEFT JOIN wisp_packages p ON p.id=i.package_id
        LEFT JOIN wisp_voucher_batches b ON b.id=i.batch_id LEFT JOIN wisp_managers m ON m.id=i.reseller_id
        WHERE '''+clause+' ORDER BY i.id DESC LIMIT 25 OFFSET ?', tuple(params)+((max(1,int(page))-1)*25,))
    return rows, total


def get_quick_issue(ident):
    return scoped_query_one('SELECT * FROM wisp_quick_card_issues WHERE id=?', (ident,))


def record_quick_print(ident):
    issue = get_quick_issue(ident)
    if not issue:
        raise PermissionError('عملية الإصدار غير متاحة.')
    with db_session() as conn:
        _VoucherWriter(conn).write('''UPDATE wisp_quick_card_issues SET print_requests=print_requests+1,
            last_print_requested_at=? WHERE id=?''',
            (get_db_storage_now().replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S'), ident))
    return issue
