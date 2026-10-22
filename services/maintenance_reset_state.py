"""Preserve old operational files outside the active generation after a reset.

Only the two fixed application-owned directories are rotated. No archive is
deleted, no job is replayed, and this function never restores SQL data.
"""
import hashlib
import json
import os
from pathlib import Path
import re


def archive_reset_state(job_id, storage=None):
    if not re.fullmatch(r'[a-f0-9]{32}', str(job_id)):
        raise ValueError('مرجع إعادة المصنع غير صالح.')
    if storage is None:
        from core.config import STORAGE_DIR
        storage = STORAGE_DIR
    root = Path(storage).resolve()
    archives = root / 'maintenance-archives'
    if archives.is_symlink():
        raise RuntimeError('تعذر تأمين أرشيف حالة التشغيل.')
    archives.mkdir(mode=0o700, exist_ok=True)
    archive = archives / job_id
    if archive.is_symlink():
        raise RuntimeError('تعذر تأمين أرشيف حالة التشغيل.')
    archive.mkdir(mode=0o700, exist_ok=True)
    manifest_path = archive / 'manifest.json'
    if manifest_path.is_symlink():
        raise RuntimeError('تعذر تأمين سجل الأرشفة.')
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('job_id') != job_id or not manifest.get('complete'):
            raise RuntimeError('لم يتأكد اكتمال أرشفة حالة التشغيل.')
        for name in ('userman-jobs', 'nas-session-settlements'):
            if (root / name).is_symlink():
                raise RuntimeError('تعذر تأمين مجلد حالة التشغيل.')
            (root / name).mkdir(mode=0o700, exist_ok=True)
        return dict(archived=True, directories=len(manifest['directories']),
                    file_count=len(manifest['files']))
    names = ('userman-jobs', 'nas-session-settlements')
    # Validate every source before changing any directory.
    for name in names:
        source, target = root / name, archive / name
        if source.is_symlink() or target.is_symlink():
            raise RuntimeError('تعذر تأمين مجلد حالة التشغيل.')
        if source.exists() and not source.is_dir():
            raise RuntimeError('مجلد حالة التشغيل غير صالح.')
        if target.exists() and source.exists():
            raise RuntimeError('تعارض حالة الأرشفة؛ لم يُستبدل أي ملف.')
        for folder in (source, target):
            if folder.exists() and any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in folder.rglob('*')):
                raise RuntimeError('أرشفة حالة التشغيل تتطلب ملفات محلية عادية.')
    moved = []
    for name in names:
        source, target = root / name, archive / name
        if source.exists():
            os.replace(source, target)
        if target.exists():
            moved.append(name)
    files = {}
    for name in moved:
        for path in (archive / name).rglob('*'):
            if path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(131072), b''):
                        digest.update(block)
                files[path.relative_to(archive).as_posix()] = digest.hexdigest()
    manifest = dict(job_id=job_id, complete=True, directories=moved, files=files)
    temporary = archive / 'manifest.tmp'
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, manifest_path)
    if os.name != 'nt':
        for directory in (archive, root):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    # Old bindings/proofs are now historical. New work starts in empty roots.
    for name in names:
        (root / name).mkdir(mode=0o700, exist_ok=True)
    return dict(archived=True, directories=len(moved), file_count=len(files))
