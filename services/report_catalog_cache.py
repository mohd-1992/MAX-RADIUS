"""Short-lived infrastructure options only; never money, counters or restricted scopes."""
import copy
import os
import threading
import time
from collections import OrderedDict

_cache=OrderedDict()
_lock=threading.Lock()
_pending={}
_epoch=0


def clear():
    global _epoch
    with _lock:
        _epoch+=1
        _cache.clear()


def invalidate():
    from services.heavy_operation_service import lock_root
    import uuid
    (lock_root()/'catalog-generation').write_text(uuid.uuid4().hex)
    clear()


def catalog_query(query, owner):
    def read(sql,params=()):
        if owner is not None or os.environ.get('MAX_REPORT_CATALOG_CACHE','1')!='1':
            return query(sql,params)
        from services.heavy_operation_service import lock_root
        marker=lock_root()/'catalog-generation'
        generation=marker.stat().st_mtime_ns if marker.exists() else 0
        key=(os.environ.get('DB_HOST'),os.environ.get('DB_NAME'),str(lock_root()),generation,_epoch,sql,tuple(params))
        now=time.monotonic()
        with _lock:
            item=_cache.get(key)
            if item and now-item[0]<30:
                _cache.move_to_end(key)
                return copy.deepcopy(item[1])
            pending=_pending.get(key)
            leader=pending is None and len(_pending)<16
            if leader:
                pending=threading.Event()
                _pending[key]=pending
        if pending is not None and not leader:
            # Bound waiting; a stalled request must not block unrelated work.
            if pending.wait(10):
                with _lock:
                    item=_cache.get(key)
                    if item and time.monotonic()-item[0]<30:
                        return copy.deepcopy(item[1])
        try:
            value=query(sql,params)
            # Avoid turning a high-cardinality catalog into a memory cache.
            if len(value)<=10000:
                with _lock:
                    _cache[key]=(time.monotonic(),copy.deepcopy(value))
                    while len(_cache)>16: _cache.popitem(last=False)
            return value
        finally:
            if leader:
                with _lock:
                    if _pending.get(key) is pending:
                        _pending.pop(key)
                    pending.set()
    return read
