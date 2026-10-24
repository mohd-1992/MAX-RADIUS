"""Migration uploads belong to tenant storage, outside the signed program tree."""
from pathlib import Path
import os
import uuid


def cache_directory():
    from core.config import STORAGE_DIR
    directory = Path(STORAGE_DIR) / 'migration_cache'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def save_backup_upload(upload):
    directory = cache_directory()
    temporary = directory / ('.upload-' + uuid.uuid4().hex)
    destination = directory / 'uploaded_backup.gz'
    try:
        upload.save(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)


def cached_backup():
    path = cache_directory() / 'uploaded_backup.gz'
    return str(path) if path.is_file() else None
