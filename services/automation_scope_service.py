"""Explicit voucher scopes, checked again on the locked account."""
def normalize_scope(parameters):
    mode = parameters.get('scope', 'all')
    if mode == 'all':
        return {'scope': 'all'}
    if mode == 'selected':
        values = parameters.get('package_ids')
        if isinstance(values, str):
            values = values.split(',')
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise ValueError('حدد معرفات الباقات المطلوبة')
        values = sorted(set(int(v) for v in values))
        if min(values) < 1:
            raise ValueError('معرف باقة غير صالح')
        return {'scope': mode, 'package_ids': values}
    if mode == 'batch':
        ident = int(parameters.get('batch_id', 0))
        if ident < 1:
            raise ValueError('حدد الحزمة المطلوبة')
        return {'scope': mode, 'batch_id': ident}
    raise ValueError('نطاق غير صالح')


def predicate(parameters, alias=''):
    p = normalize_scope(parameters or {})
    prefix = alias + '.' if alias else ''
    if p['scope'] == 'selected':
        return prefix + 'package_id IN (' + ','.join('?' for _ in p['package_ids']) + ')', tuple(p['package_ids'])
    if p['scope'] == 'batch':
        return prefix + 'batch_id=?', (p['batch_id'],)
    return '1=1', ()


def matches(row, parameters):
    p = normalize_scope(parameters or {})
    if p['scope'] == 'selected':
        return int(row.get('package_id') or 0) in p['package_ids']
    if p['scope'] == 'batch':
        return int(row.get('batch_id') or 0) == p['batch_id']
    return True
