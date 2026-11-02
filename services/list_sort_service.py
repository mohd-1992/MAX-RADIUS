"""Allowlisted ordering for the three administrative account lists."""
from decimal import Decimal, InvalidOperation

ACCOUNT_FIELDS = {
    'id':'seq_id', 'fullname':'full_name', 'username':'username', 'days':'sort_remaining_seconds',
    'lastseen':'last_seen_str', 'package':'package_name', 'accesspoint':'access_point',
    'consumption':'sort_used_bytes', 'expires':'expires_at', 'ip':'ip_address',
    'balance':'balance', 'email':'email', 'phone':'phone', 'createdby':'created_by_name',
    'createdat':'created_at', 'notes':'notes', 'service':'service_type',
    'batch':'batch_name', 'mac':'mac_binding', 'reseller':'reseller_name',
}
BATCH_FIELDS = {'name':'name', 'package':'package_name', 'reseller':'reseller_name',
                'count':'card_count', 'states':'active_count', 'createdat':'created_at'}
FIELDS = {
    'subscribers': {k:v for k,v in ACCOUNT_FIELDS.items() if k not in ('batch','mac','reseller')},
    'cards': {k:v for k,v in ACCOUNT_FIELDS.items() if k not in ('fullname','email','phone','createdby')},
    'batches': dict(BATCH_FIELDS, id='id'),
}
FIELDS['cards']['mac'] = 'bound_mac'
# These columns can be ordered before card hydration and LIMIT/OFFSET.
CARD_SQL = {'id':'COALESCE(v.global_seq_id,v.id)', 'username':'v.username',
    'package':'p.name', 'expires':'v.expires_at', 'balance':'v.balance',
    'batch':'b.name', 'mac':'v.bound_mac', 'reseller':'r.name',
    'createdat':'COALESCE(v.first_used_at,v.created_at)',
    'service':"COALESCE(p.service_type,'hotspot')"}


def sort_state(args, table):
    key, direction = args.get('list_sort','id'), args.get('list_dir','desc')
    if key not in FIELDS[table] or direction not in ('asc','desc'):
        key, direction = 'id', 'desc'
    return dict(key=key, direction=direction, table=table)


def card_order(state):
    expression = CARD_SQL.get(state['key'])
    if expression is None:
        return None
    return f'({expression} IS NULL) ASC,{expression} {state["direction"].upper()},v.id DESC'


def sort_rows(rows, state):
    field = FIELDS[state['table']][state['key']]
    numeric = state['key'] in ('id','days','consumption','balance','count','states')
    def value(row):
        raw = row.get(field)
        if state['key']=='createdat' and state['table']=='cards':
            raw = row.get('first_used_at') or raw
        if state['key']=='lastseen' and row.get('is_online'):
            return '9999'
        if raw is None or str(raw).strip() in ('','-','لم يتصل بعد','غير متاح'):
            return None
        if numeric:
            try:
                return Decimal(str(raw))
            except InvalidOperation:
                return None
        return str(raw).casefold()
    # Missing values stay last in both directions; original IDs break all ties.
    ordered = sorted(rows, key=lambda row: int(row.get('id') or 0), reverse=True)
    known = [row for row in ordered if value(row) is not None]
    missing = [row for row in ordered if value(row) is None]
    return sorted(known, key=value, reverse=state['direction']=='desc') + missing
