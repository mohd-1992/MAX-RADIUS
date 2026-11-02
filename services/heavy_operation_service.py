"""Cross-process admission control for expensive work, outside accounting tables."""
import contextlib
import contextvars
import functools
import os
import tempfile
from pathlib import Path

_held = contextvars.ContextVar('max_heavy_held', default=False)
_cancel_file = contextvars.ContextVar('max_export_cancel_file', default=None)


class OperationBusy(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    pass


def lock_root():
    # All Web workers/replicas for a domain must share STORAGE_DIR.
    root = Path(os.environ.get('STORAGE_DIR') or (Path(__file__).resolve().parents[1] / 'storage'))
    target = root / 'operation-locks'
    target.mkdir(parents=True, exist_ok=True)
    return target


@contextlib.contextmanager
def operation_slot():
    if _held.get():
        yield
        return
    path = lock_root() / 'heavy.lock'
    with path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            raise OperationBusy('توجد عملية تصدير أو استيراد أو نسخ قيد التنفيذ. انتظر اكتمالها ثم أعد المحاولة.') from None
        token = _held.set(True)
        try:
            yield
        finally:
            _held.reset(token)
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def heavy_operation(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        with operation_slot():
            if func.__name__ in ('restore_backup','execute_database_migration'):
                from services.report_catalog_cache import invalidate
                from services.session_control_service import maintenance
                with maintenance():
                    invalidate()
                    try: return func(*args, **kwargs)
                    finally: invalidate()
            with export_job() if func.__name__ in ('print_cards','session_pdf_response') else contextlib.nullcontext():
                return func(*args, **kwargs)
    return wrapped


@contextlib.contextmanager
def export_job():
    from flask import has_request_context, request, session
    import json
    import re
    token=request.headers.get('X-Max-Export','') if has_request_context() else ''
    if not token:
        yield
        return
    if not re.fullmatch('[0-9a-f]{32}',token) or not session.get('admin_id'):
        raise ValueError('معرف عملية التصدير غير صالح.')
    state=lock_root()/(token+'.json');cancel=lock_root()/(token+'.cancel')
    with state.open('x') as file:json.dump({'owner':session['admin_id']},file)
    marker=_cancel_file.set(cancel)
    try:yield
    finally:
        _cancel_file.reset(marker)
        state.unlink(missing_ok=True);cancel.unlink(missing_ok=True)


def export_cancelled():
    path=_cancel_file.get()
    return bool(path and path.exists())


def cancel_export(token,owner):
    import json
    import re
    if not re.fullmatch('[0-9a-f]{32}',token):return False
    state=lock_root()/(token+'.json')
    try:
        if json.loads(state.read_text())['owner']!=owner:return False
    except (OSError,ValueError,KeyError):return False
    cancel=lock_root()/(token+'.cancel');cancel.touch()
    if not state.exists():
        cancel.unlink(missing_ok=True);return False
    return True


def temporary_output():
    # File-backed output, deleted on close (including aborted HTTP responses).
    return tempfile.TemporaryFile(mode='w+b')
