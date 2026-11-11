"""Capture the cost of a new invoice independently of the collected amount."""
from decimal import Decimal,InvalidOperation


def record_invoice_cost(conn, invoice_id, package):
    from database.db import adapt_query
    value=package.get('cost')
    if value is not None:
        try:
            value=Decimal(str(value))
            if not value.is_finite() or value<0 or value>Decimal('9999999999.99'):
                raise ValueError('تكلفة الباقة غير صالحة.')
            if value!=value.quantize(Decimal('.01')):raise ValueError('دقة تكلفة الباقة غير صالحة.')
        except InvalidOperation as exc:raise ValueError('تكلفة الباقة غير صالحة.') from exc
    kind=package.get('service_type') or ''
    if kind not in ('hotspot','pppoe','both'):kind=''
    with conn.cursor() as cur:
        cur.execute(adapt_query('''UPDATE wisp_invoices SET report_cost=?,cost_source=?,package_service_type=?
            WHERE id=? AND report_cost IS NULL AND cost_source='' ''',conn),
            (value,'package_snapshot' if value is not None else 'unavailable',kind,invoice_id))
