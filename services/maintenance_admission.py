"""Bounded admission before maintenance starts; never replay an operation."""
from contextlib import ExitStack, contextmanager
import logging
import math
import time

from database.db import get_connection
from services.account_lifecycle_service import job_lock, sql

log = logging.getLogger(__name__)
AUTOMATION_WAIT_SECONDS = 120


def _describe_owners(names):
    """Diagnostic reads only; never release another connection's advisory lock."""
    conn = get_connection()
    try:
        sql(conn, 'SET SESSION max_statement_time=1')
        sql(conn, 'SET SESSION lock_wait_timeout=1')
        owners = []
        for name in names:
            row = sql(conn, 'SELECT IS_USED_LOCK(CONCAT(DATABASE(), ?)) AS owner', (':' + name,), 'one')
            owner = row.get('owner') if row else None
            process = sql(conn, 'SELECT COMMAND,TIME,STATE FROM information_schema.PROCESSLIST '
                          'WHERE ID=? AND DB=DATABASE()', (owner,), 'one') if owner else None
            owners.append(dict(lock=name, connection=owner, process=process))
        return owners
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def maintenance_admission(names, *, timeout=0, progress=None, cancelled=None):
    """Yield one admission receipt while holding all required locks, or none.

    All attempts are nonblocking. Release a partially acquired group before
    waiting. The operation runs outside the retry loop: an exception after
    admission propagates without a second execution or a second acquisition.
    """
    names = tuple(names)
    timeout = float(timeout)
    if not names or len(set(names)) != len(names) or not math.isfinite(timeout) or not 0 <= timeout <= 300:
        raise ValueError('Invalid maintenance admission bounds')
    started = time.monotonic()
    next_notice = started
    next_log = started
    stop = cancelled or (lambda: False)
    while True:
        was_cancelled = bool(stop())
        blocked = []
        with ExitStack() as stack:
            if not was_cancelled:
                for name in names:
                    if not stack.enter_context(job_lock(name)):
                        blocked.append(name)
                        break
                was_cancelled = bool(stop())
            if not blocked and not was_cancelled:
                yield dict(acquired=True)
                return
        # No maintenance lock is held across a diagnostic read, heartbeat or sleep.
        now = time.monotonic()
        waited = now - started
        if was_cancelled:
            yield dict(acquired=False, success=False, cancelled=True, processed=0,
                       message='ألغيت المهمة أثناء انتظار الصيانة؛ لم يبدأ التنفيذ')
            return
        if now >= next_log:
            try:
                owners = _describe_owners(blocked)
            except Exception:
                owners = 'owner observation unavailable'
            log.warning('Maintenance admission waiting: blocked=%s owners=%s', blocked, owners)
            next_log = now + 30
        if waited >= timeout:
            yield dict(acquired=False, success=False, deferred=True, processed=0,
                       message='أُجلت المهمة لانشغال الصيانة بعد انتظار محدود؛ لم يبدأ التنفيذ. راجع السجل قبل تشغيلها مجددًا')
            return
        if progress and now >= next_notice:
            progress(dict(processed=0, message='بانتظار اكتمال عملية صيانة أخرى؛ لم يبدأ التنفيذ'))
            next_notice = now + 2
        time.sleep(min(.5, timeout - waited))
