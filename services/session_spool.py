"""Private, disk-backed session batches; no external serialized input is read."""
import base64
import datetime
from decimal import Decimal
import json
import tempfile


def _encode(value):
    if isinstance(value, datetime.datetime):
        kind, text = 'datetime', value.isoformat()
    elif isinstance(value, datetime.date):
        kind, text = 'date', value.isoformat()
    elif isinstance(value, Decimal):
        kind, text = 'decimal', str(value)
    elif isinstance(value, bytes):
        kind, text = 'bytes', base64.b64encode(value).decode('ascii')
    else:
        raise TypeError('Unsupported session spool value type.')
    return {'_max_session_type': kind, 'value': text}


def _decode(value):
    if set(value) != {'_max_session_type', 'value'}:
        return value
    kind, text = value['_max_session_type'], value['value']
    if kind == 'datetime':
        return datetime.datetime.fromisoformat(text)
    if kind == 'date':
        return datetime.date.fromisoformat(text)
    if kind == 'decimal':
        return Decimal(text)
    if kind == 'bytes':
        return base64.b64decode(text, validate=True)
    raise ValueError('Unsupported internal session spool type.')


class SessionSpool:
    def __init__(self, directory=None):
        self._stream = tempfile.TemporaryFile(mode='w+b', dir=directory)
        self.total = 0
        self.size_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stream.close()

    def append(self, rows):
        for row in rows:
            payload = json.dumps(row, default=_encode, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8') + b'\n'
            self._stream.write(payload)
            self.size_bytes += len(payload)
            self.total += 1

    def batches(self, size):
        if size <= 0:
            raise ValueError('Session batch size must be positive.')
        self._stream.seek(0)
        rows = []
        for line in self._stream:
            rows.append(json.loads(line, object_hook=_decode))
            if len(rows) == size:
                yield rows
                rows = []
        if rows or not self.total:
            yield rows
