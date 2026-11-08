"""Shared notification-center layout; formatting only, no database or delivery."""
from datetime import date
from decimal import Decimal


def format_sales_summary(period, start, end, *, voucher_count, voucher_revenue,
                         invoice_count=None, invoice_revenue=None,
                         active_subscribers=None, online_sessions=None,
                         legacy_daily=False):
    labels = {'day': ('اليومي', 'Daily_Sales'),
              'week': ('الأسبوعي', 'Weekly_Sales'),
              'month': ('الشهري', 'Monthly_Sales')}
    title, hashtag = labels[period]
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if end < start:
        raise ValueError('Invalid report period')
    revenue = Decimal(str(voucher_revenue))
    has_invoices = invoice_count is not None
    if has_invoices:
        revenue += Decimal(str(invoice_revenue))
    date_line = (f'📅 <b>التاريخ:</b> <code>{start}</code>' if start == end else
                 f'📅 <b>الفترة:</b> <code>{start}</code> — <code>{end}</code>')
    if period == 'month':
        months = ('يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
                  'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر')
        date_line = f'📅 <b>مبيعات شهر {months[start.month - 1]} {start.year}</b>'
    lines = [f'📊 <b>التقرير المالي والمبيعات {title} - MAX RADIUS</b>',
             '━━━━━━━━━━━━━━━━━━━', date_line]
    if legacy_daily:
        lines += [f'🎫 <b>الكروت المفعلة اليوم:</b> <b>{int(voucher_count)} كرت</b>',
                  f'💵 <b>إجمالي الإيرادات اليوم:</b> <b>{revenue:,.2f} ريال</b>']
    else:
        lines += [f'🎫 <b>عمليات مبيعات الكروت:</b> <b>{int(voucher_count)} عملية</b>',
                  f'💵 <b>إيرادات الكروت:</b> <b>{Decimal(str(voucher_revenue)):,.2f} ريال</b>']
        if has_invoices:
            lines += [f'🧾 <b>الفواتير المدفوعة:</b> <b>{int(invoice_count)} عملية</b>',
                      f'💳 <b>إيرادات الفواتير:</b> <b>{Decimal(str(invoice_revenue)):,.2f} ريال</b>']
        lines.append(f'💰 <b>إجمالي الإيرادات:</b> <b>{revenue:,.2f} ريال</b>')
    if active_subscribers is not None:
        lines.append(f'👥 <b>المشتركون النشطون:</b> <b>{int(active_subscribers)} مشترك</b>')
    if online_sessions is not None:
        lines.append(f'📶 <b>المتصلون الآن بالشبكة:</b> <b>{int(online_sessions)} جلسة</b>')
    lines += ['━━━━━━━━━━━━━━━━━━━', f'#{hashtag} #Financial_Report #MAX_RADIUS']
    return '\n'.join(lines)
