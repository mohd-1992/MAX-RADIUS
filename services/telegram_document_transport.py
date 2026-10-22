"""Bounded streaming document uploads; no application, database or worker imports."""
import json
import contextlib
import logging
import os
import re
import threading
import time

import httpx


_UPLOAD_SLOT = threading.BoundedSemaphore(1)
_MAX_REPLY = 1024 * 1024


class _RedactBotURL(logging.Filter):
    def filter(self, record):
        # HTTPX INFO logs contain the request URL, whose path contains the token.
        message = record.getMessage()
        record.msg = re.sub(r'(/bot)[^/\s\"\']+', r'\1[REDACTED]', message)
        record.args = ()
        return True


logging.getLogger('httpx').addFilter(_RedactBotURL())


class UploadFailure(Exception):
    def __init__(self, code, phase, progress, uncertain=False):
        super().__init__(code)
        self.details = dict(code=code, phase=phase, outcome_uncertain=uncertain,
                            **progress)


class _DeadlineStream(httpx.SyncByteStream):
    def __init__(self, source, deadline, clock, progress):
        self.source, self.deadline = source, deadline
        self.clock, self.progress = clock, progress

    def __iter__(self):
        for chunk in self.source:
            if self.clock() >= self.deadline:
                raise UploadFailure('upload_deadline', 'upload', self.progress, True)
            # This is local transport progress, not a Telegram delivery receipt.
            self.progress['body_bytes_read'] += len(chunk)
            yield chunk
        if self.clock() >= self.deadline:
            raise UploadFailure('upload_deadline', 'upload', self.progress, True)

    def close(self):
        self.source.close()


def upload_document(token, chat_id, filepath, caption, *, connect_timeout=10.0,
                    write_timeout=60.0, read_timeout=60.0, total_timeout=600.0,
                    _endpoint=None, _transport=None, _clock=time.monotonic,
                    _file=None, _filename=None):
    """Send one file; retry only a definite Telegram 400 HTML rejection.

    The elapsed deadline is checked between streaming chunks. A blocked operation
    is additionally bounded by its own connect/write/read timeout, so shutdown
    can exceed the elapsed budget by at most one I/O timeout. Never retry an
    unknown outcome, redirect the token, or monopolize a DB pool connection.
    Underscored injection points are for isolated tests, not user configuration.
    """
    if min(connect_timeout, write_timeout, read_timeout, total_timeout) <= 0:
        raise ValueError('Upload timeouts must be positive')
    progress = {'body_bytes_read': 0, 'attempts': 0, 'elapsed_seconds': 0.0}
    if not _UPLOAD_SLOT.acquire(blocking=False):
        raise UploadFailure('upload_busy', 'queue', progress)
    start = _clock()
    phase = 'connect'
    try:
        endpoint = _endpoint or 'https://api.telegram.org/bot' + token + '/sendDocument'
        deadline = start + total_timeout
        timeout = httpx.Timeout(connect=connect_timeout, write=write_timeout,
                                read=read_timeout, pool=connect_timeout)
        with httpx.Client(timeout=timeout, follow_redirects=False,
                          transport=_transport) as client:
            with (open(filepath, 'rb') if _file is None else contextlib.nullcontext(_file)) as file:
                for attempt in range(2):
                    if _clock() >= deadline:
                        raise UploadFailure('upload_deadline', phase, progress, attempt > 0)
                    file.seek(0)
                    fields = {'chat_id': str(chat_id), 'caption': caption}
                    if attempt == 0:
                        fields['parse_mode'] = 'HTML'
                    request = client.build_request('POST', endpoint, data=fields,
                        files={'document': (_filename or os.path.basename(filepath), file,
                                            'application/octet-stream')})
                    request.stream = _DeadlineStream(request.stream, deadline,
                                                     _clock, progress)
                    progress['attempts'] += 1
                    phase = 'upload'
                    response = client.send(request, stream=True)
                    try:
                        phase = 'response'
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            if _clock() >= deadline:
                                raise UploadFailure('upload_deadline', phase, progress, True)
                            size += len(chunk)
                            if size > _MAX_REPLY:
                                raise UploadFailure('reply_too_large', phase, progress, True)
                            chunks.append(chunk)
                        try:
                            result = json.loads(b''.join(chunks))
                        except (ValueError, UnicodeError):
                            raise UploadFailure('invalid_reply', phase, progress, True) from None
                        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                            raise UploadFailure('invalid_reply', phase, progress, True)
                        if response.status_code >= 300 and result.get('ok'):
                            raise UploadFailure('invalid_reply', phase, progress, True)
                        html_rejected = (response.status_code == 400 and
                            result.get('ok') is False and result.get('error_code') == 400 and
                            "can't parse entities" in str(result.get('description', '')).lower())
                        if attempt == 0 and html_rejected:
                            continue  # Explicit rejection proves no document was accepted.
                        progress['elapsed_seconds'] = round(_clock() - start, 3)
                        return result, dict(progress)
                    finally:
                        response.close()
        raise UploadFailure('invalid_reply', phase, progress, True)
    except UploadFailure as exc:
        exc.details['elapsed_seconds'] = round(_clock() - start, 3)
        raise
    except (httpx.HTTPError, OSError) as exc:
        progress['elapsed_seconds'] = round(_clock() - start, 3)
        # Raw exceptions/URLs must never expose bot credentials in logs or UI.
        code = ('connect_timeout' if isinstance(exc, httpx.ConnectTimeout) else
                'write_timeout' if isinstance(exc, httpx.WriteTimeout) else
                'read_timeout' if isinstance(exc, httpx.ReadTimeout) else 'connection_error')
        actual_phase = ('connect' if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                        else 'response' if isinstance(exc, (httpx.ReadTimeout,
                            httpx.ReadError, httpx.RemoteProtocolError)) else phase)
        raise UploadFailure(code, actual_phase, progress,
                            actual_phase != 'connect') from None
    finally:
        _UPLOAD_SLOT.release()
