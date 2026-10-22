"""Virtual backup parts plus a self-contained verifier; never modifies the archive."""
import hashlib
import io
import json
import math
import os
import threading
import time

from services.telegram_document_transport import upload_document, UploadFailure


PART_BYTES = 45_000_000
MAX_PARTS = 256
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024 * 1024
_BUNDLE_SLOT = threading.BoundedSemaphore(1)

# Public, dependency-free utility sent with large backups. No server credentials,
# network, application imports, database access, or automatic restore operations.
JOIN_TOOL_SOURCE = r'''"""Usage: python join_max_backup.py <filename.parts.json>"""
from pathlib import Path
import hashlib, json, os, sys, tempfile

def safe_name(value):
    if not isinstance(value, str) or not value or value in ('.', '..') or any(c in value for c in '/\\\x00\r\n:'):
        raise ValueError('Unsafe file name')
    return value

def join_backup(manifest_path):
    path = Path(manifest_path).resolve()
    folder = path.parent
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('format') != 'max-radius-backup-parts-v1':
        raise ValueError('Unknown manifest format')
    name = safe_name(manifest['filename'])
    size = manifest['size_bytes']
    parts = manifest['parts']
    if type(size) is not int or not 0 < size <= 5*1024*1024*1024 or not isinstance(parts, list) or not 1 <= len(parts) <= 256:
        raise ValueError('Invalid manifest size or parts')
    names = [safe_name(p['filename']) for p in parts]
    if len(set(names)) != len(names) or any(type(p.get('index')) is not int or p['index'] != i+1 for i,p in enumerate(parts)):
        raise ValueError('Duplicate or out-of-order parts')
    if any(type(p.get('size_bytes')) is not int or not 0 < p['size_bytes'] <= 45_000_000 for p in parts) or sum(p['size_bytes'] for p in parts) != size:
        raise ValueError('Part sizes do not match original')
    target = folder/name
    if target.exists():
        raise FileExistsError('Output already exists; no file overwritten')
    fd, temporary = tempfile.mkstemp(prefix='.joining-', dir=folder)
    temp = Path(temporary)
    try:
        aggregate = hashlib.sha256()
        with os.fdopen(fd, 'wb') as out:
            for part in parts:
                source = folder/part['filename']
                if source.is_symlink() or source.resolve().parent != folder or source.stat().st_size != part['size_bytes']:
                    raise ValueError('Missing, unsafe or incorrect part size')
                digest = hashlib.sha256()
                with source.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(65536), b''):
                        digest.update(chunk); aggregate.update(chunk); out.write(chunk)
                if digest.hexdigest() != part['sha256']:
                    raise ValueError('Part checksum mismatch')
            if out.tell() != size or aggregate.hexdigest() != manifest['sha256']:
                raise ValueError('Original checksum mismatch')
            out.flush(); os.fsync(out.fileno())
        # Atomic no-overwrite publication; all original parts remain untouched.
        os.link(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Usage: python join_max_backup.py <filename.parts.json>')
        output = join_backup(sys.argv[1])
        print('Verified backup ready: ' + output.name)
    except Exception as error:
        print('Backup not assembled: ' + str(error), file=sys.stderr)
        sys.exit(1)
'''


class PartReader(io.RawIOBase):
    """A seekable bounded view; HTTPX sees the part length, not the full archive."""
    def __init__(self, path, offset, length):
        super().__init__()
        self.source = open(path, 'rb')
        self.offset, self.length, self.position = offset, length, 0
        self.source.seek(offset)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def fileno(self):
        raise io.UnsupportedOperation('Virtual part, not a whole file descriptor')

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position+offset if whence == 1 else self.length+offset
        if whence not in (0, 1, 2) or not 0 <= position <= self.length:
            raise ValueError('Seek outside virtual part')
        self.source.seek(self.offset+position)
        self.position = position
        return position

    def read(self, size=-1):
        remaining = self.length-self.position
        count = remaining if size is None or size < 0 else min(size, remaining)
        # Keep network reads bounded even if a caller requests the entire part.
        count = min(count, 65536)
        chunk = self.source.read(count)
        if count and not chunk:
            raise OSError('Backup changed or truncated while uploading')
        self.position += len(chunk)
        return chunk

    def close(self):
        if not self.closed:
            self.source.close()
        super().close()


def plan_parts(filepath, *, part_bytes=PART_BYTES):
    size = os.path.getsize(filepath)
    if type(part_bytes) is not int or not 0 < part_bytes <= PART_BYTES or not 0 < size <= MAX_ARCHIVE_BYTES or math.ceil(size/part_bytes) > MAX_PARTS:
        raise ValueError('Backup exceeds bounded part count or has invalid size')
    aggregate, parts = hashlib.sha256(), []
    with open(filepath, 'rb') as source:
        for index in range(math.ceil(size/part_bytes)):
            count, digest = 0, hashlib.sha256()
            while count < min(part_bytes, size-index*part_bytes):
                chunk = source.read(min(65536, part_bytes-count, size-index*part_bytes-count))
                if not chunk:
                    raise OSError('Backup truncated during checksum preparation')
                aggregate.update(chunk); digest.update(chunk); count += len(chunk)
            parts.append({'index': index+1, 'size_bytes': count, 'sha256': digest.hexdigest()})
        if source.read(1):
            raise OSError('Backup grew during checksum preparation')
    name = os.path.basename(filepath)
    prefix = name+'.'+aggregate.hexdigest()[:12]
    for part in parts:
        part['filename'] = prefix+'.part'+str(part['index']).zfill(3)
    return {'format': 'max-radius-backup-parts-v1', 'filename': name,
            'size_bytes': size, 'sha256': aggregate.hexdigest(), 'parts': parts}


def send_backup_document(token, chat_id, filepath, caption, *, _part_bytes=PART_BYTES,
                         _send=None, _clock=time.monotonic,
                         _bundle_timeout=7200.0):
    """One completion log per archive; stop on uncertainty, never replay a bundle."""
    if not _BUNDLE_SLOT.acquire(blocking=False):
        raise UploadFailure('upload_busy', 'queue', {}, False)
    start = _clock()
    confirmed = []
    try:
        _send = _send or upload_document
        size = os.path.getsize(filepath)
        if size <= _part_bytes:
            return _send(token, chat_id, filepath, caption,
                total_timeout=min(_bundle_timeout, max(600.0, size/32768.0+120.0)))
        original_stat = os.stat(filepath)
        manifest = plan_parts(filepath, part_bytes=_part_bytes)
        current = os.stat(filepath)
        if current.st_size != original_stat.st_size or current.st_mtime_ns != original_stat.st_mtime_ns:
            raise UploadFailure('backup_changed', 'prepare', {}, False)
        manifest_name = manifest['parts'][0]['filename'].rsplit('.part', 1)[0]+'.parts.json'
        def send(file, filename, text, length):
            remaining = _bundle_timeout-(_clock()-start)
            if remaining <= 0:
                raise UploadFailure('bundle_deadline', 'upload', {}, False)
            response, progress = _send(token, chat_id, filepath, text,
                _file=file, _filename=filename,
                total_timeout=min(remaining, max(600.0, length/32768.0+120.0)))
            if not response.get('ok'):
                raise UploadFailure('telegram_rejected_part', 'response',
                    {'telegram_error_code': response.get('error_code')}, False)
            confirmed.append({'filename': filename,
                              'message_id': (response.get('result') or {}).get('message_id')})
            return response, progress
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        with io.BytesIO(payload) as data:
            send(data, manifest_name, 'ملف تحقق أجزاء النسخة؛ يلزم وصول جميع الأجزاء للتجميع.', len(payload))
        offset = 0
        for part in manifest['parts']:
            current = os.stat(filepath)
            if current.st_size != original_stat.st_size or current.st_mtime_ns != original_stat.st_mtime_ns:
                raise UploadFailure('backup_changed', 'prepare', {}, False)
            with PartReader(filepath, offset, part['size_bytes']) as view:
                send(view, part['filename'],
                    f"جزء {part['index']}/{len(manifest['parts'])} من النسخة {manifest['filename']}",
                    part['size_bytes'])
            current = os.stat(filepath)
            if current.st_size != original_stat.st_size or current.st_mtime_ns != original_stat.st_mtime_ns:
                raise UploadFailure('backup_changed', 'upload', {}, False)
            offset += part['size_bytes']
        tool = JOIN_TOOL_SOURCE.encode('utf-8')
        with io.BytesIO(tool) as data:
            response, progress = send(data, 'join_max_backup.py',
                'ضع الأجزاء وملف التحقق في مجلد واحد ثم شغّل: python join_max_backup.py "'+manifest_name+'". لا تستعد أي جزء منفرد.',
                len(tool))
        return dict(response, _bundle={'filename': manifest['filename'], 'size_bytes': size,
            'sha256': manifest['sha256'], 'part_count': len(manifest['parts']),
            'confirmed_documents': confirmed}), dict(progress, bundle_complete=True)
    except UploadFailure as exc:
        exc.details['confirmed_documents'] = confirmed
        exc.details['bundle_complete'] = False
        raise
    except (OSError, ValueError):
        raise UploadFailure('backup_prepare_failed', 'prepare',
            {'confirmed_documents': confirmed, 'bundle_complete': False}, False) from None
    finally:
        _BUNDLE_SLOT.release()
