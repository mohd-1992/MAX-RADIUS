def split_sql_statements(sql_text):
    """Splits a multi-statement SQL script into individual executable statements respecting quotes and comments."""
    statements = []
    current = []
    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    in_line_comment = False
    in_block_comment = False
    escape = False

    i = 0
    length = len(sql_text)
    while i < length:
        ch = sql_text[i]
        next_ch = sql_text[i+1] if i + 1 < length else ''

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == '*' and next_ch == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if escape:
            current.append(ch)
            escape = False
            i += 1
            continue

        if ch == '\\':
            current.append(ch)
            escape = True
            i += 1
            continue

        if ch == "'" and not in_double_quote and not in_backtick:
            in_single_quote = not in_single_quote
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single_quote and not in_backtick:
            in_double_quote = not in_double_quote
            current.append(ch)
            i += 1
            continue

        if ch == '`' and not in_single_quote and not in_double_quote:
            in_backtick = not in_backtick
            current.append(ch)
            i += 1
            continue

        if not in_single_quote and not in_double_quote and not in_backtick:
            if ch == '-' and next_ch == '-':
                in_line_comment = True
                i += 2
                continue
            if ch == '#' and (i == 0 or (i > 0 and sql_text[i-1] in '\r\n ')):
                in_line_comment = True
                i += 1
                continue
            if ch == '/' and next_ch == '*':
                in_block_comment = True
                i += 2
                continue
            if ch == ';':
                stmt = ''.join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    remaining = ''.join(current).strip()
    if remaining:
        statements.append(remaining)

    return statements


# -*- coding: utf-8 -*-
"""
Universal & Comprehensive Backup Management Service for MAX RADIUS (FreeRADIUS & WISP Manager).
Handles full MySQL database dumps, uploads/attachments packaging, card designs & settings archive,
atomic multi-component restores, and settings management.
"""

import os
import io
import json
import tarfile
import zipfile
import hashlib
import sqlite3
import datetime
import shutil
import secrets
from pathlib import Path
from database.db import DB_PATH, get_connection, db_session, is_mysql_conn, execute_write, execute_update, query_one, query_all, log_audit
from core.config import BACKUPS_DIR, STORAGE_DIR, UPLOADS_DIR, DB_TYPE, APP_VERSION

CURRENT_SYSTEM_VERSION = APP_VERSION
DEFAULT_BACKUP_DIR = str(BACKUPS_DIR)

def get_backup_dir():
    """Retrieve current backup directory from system settings or fallback to default."""
    try:
        rec = query_one("SELECT `value` FROM wisp_system_settings WHERE `key` = 'backup_storage_path'")
        if rec and rec.get('value') and rec['value'].strip():
            custom_dir = rec['value'].strip()
            if os.path.exists(custom_dir) or os.access(os.path.dirname(custom_dir) or '.', os.W_OK):
                os.makedirs(custom_dir, exist_ok=True)
                return custom_dir
    except Exception:
        pass
    
    if not os.path.exists(DEFAULT_BACKUP_DIR):
        os.makedirs(DEFAULT_BACKUP_DIR, exist_ok=True)
    return DEFAULT_BACKUP_DIR

def ensure_backup_dir():
    """Ensure the backup storage directory exists."""
    b_dir = get_backup_dir()
    if not os.path.exists(b_dir):
        os.makedirs(b_dir, exist_ok=True)
    return b_dir

def get_file_sha256(filepath):
    """Compute SHA256 checksum of a file."""
    sha = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()

def get_backup_settings():
    """Retrieve all automated backup configuration from system settings."""
    settings = {
        'auto_enabled': False,
        'interval_days': 1,
        'times': ['03:00'],
        'storage_path': get_backup_dir(),
        'last_run': None,
        'last_status': None,
        'last_message': ''
    }
    
    try:
        rows = query_all("SELECT `key`, `value` FROM wisp_system_settings WHERE `key` LIKE ?", ('backup_%',))
        db_map = {r['key']: r['value'] for r in rows}
        
        if 'backup_auto_enabled' in db_map:
            settings['auto_enabled'] = str(db_map['backup_auto_enabled']).lower() in ['1', 'true', 'yes', 'on']
        if 'backup_interval_days' in db_map:
            try:
                settings['interval_days'] = max(1, int(db_map['backup_interval_days']))
            except Exception:
                settings['interval_days'] = 1
        if 'backup_times' in db_map and db_map['backup_times']:
            try:
                parsed_times = json.loads(db_map['backup_times'])
                if isinstance(parsed_times, list) and parsed_times:
                    settings['times'] = parsed_times[:6]
            except Exception:
                # Comma separated fallback
                raw = [t.strip() for t in db_map['backup_times'].split(',') if t.strip()]
                if raw:
                    settings['times'] = raw[:6]
        if 'backup_storage_path' in db_map and db_map['backup_storage_path'].strip():
            settings['storage_path'] = db_map['backup_storage_path'].strip()
        if 'backup_last_run' in db_map:
            settings['last_run'] = db_map['backup_last_run']
        if 'backup_last_status' in db_map:
            settings['last_status'] = db_map['backup_last_status']
        if 'backup_last_message' in db_map:
            settings['last_message'] = db_map['backup_last_message']
    except Exception:
        pass
        
    return settings

def save_backup_settings(auto_enabled, interval_days, times, storage_path, admin_username='admin'):
    """Save automated backup configuration to database and notify scheduler."""
    auto_enabled_str = 'true' if auto_enabled else 'false'
    interval_days = max(1, int(interval_days or 1))
    
    # Filter and validate up to 6 time strings (HH:MM)
    cleaned_times = []
    if isinstance(times, str):
        try:
            times = json.loads(times)
        except Exception:
            times = [t.strip() for t in times.split(',') if t.strip()]
            
    for t in (times or []):
        t_str = str(t).strip()
        if len(t_str) == 5 and ':' in t_str:
            cleaned_times.append(t_str)
            if len(cleaned_times) >= 6:
                break
                
    if not cleaned_times:
        cleaned_times = ['03:00']
        
    times_json = json.dumps(cleaned_times)
    storage_path = str(storage_path or '').strip()
    if storage_path:
        os.makedirs(storage_path, exist_ok=True)
        
    settings_to_update = {
        'backup_auto_enabled': (auto_enabled_str, 'تفعيل النسخ الاحتياطي التلقائي'),
        'backup_interval_days': (str(interval_days), 'الفاصل الزمني للنسخ التلقائي بالأيام'),
        'backup_times': (times_json, 'أوقات تشغيل النسخ الاحتياطي اليومية'),
        'backup_storage_path': (storage_path, 'مسار حفظ النسخ الاحتياطية')
    }
    
    for k, (v, desc) in settings_to_update.items():
        existing = query_one("SELECT `key` FROM wisp_system_settings WHERE `key` = ?", (k,))
        if existing:
            execute_update("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?", (v, k))
        else:
            execute_write("INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES (?, ?, ?)", (k, v, desc))
            
    log_audit(1, admin_username, 'SAVE_BACKUP_SETTINGS', 'backup', f'Updated backup settings (enabled: {auto_enabled_str}, times: {times_json})')
    
    # Reload background scheduler
    try:
        from services.backup_scheduler_service import reload_backup_schedule
        reload_backup_schedule()
    except Exception:
        pass
        
    return True, "تم حفظ إعدادات النسخ الاحتياطي التلقائي وتحديث المجدول بنجاح."

def sync_disk_backups():
    """Sync physical files in backup directory with database records."""
    b_dir = ensure_backup_dir()
    if not os.path.exists(b_dir):
        return
        
    existing_files = os.listdir(b_dir)
    for fname in existing_files:
        if not (fname.endswith('.tar.gz') or fname.endswith('.tgz') or fname.endswith('.zip') or fname.endswith('.sql.gz') or fname.endswith('.db') or fname.endswith('.sql')):
            continue
            
        fpath = os.path.join(b_dir, fname)
        if not os.path.isfile(fpath):
            continue
            
        size_bytes = os.path.getsize(fpath)
        size_mb = round(size_bytes / (1024 * 1024), 2)
        
        # Check if already in DB
        db_rec = query_one("SELECT id FROM wisp_backups WHERE filename = ?", (fname,))
        if not db_rec:
            sys_version = CURRENT_SYSTEM_VERSION
            created_at = datetime.datetime.fromtimestamp(os.path.getmtime(fpath)).strftime('%Y-%m-%d %H:%M:%S')
            checksum = ""
            
            if fname.endswith('.tar.gz') or fname.endswith('.tgz'):
                try:
                    with tarfile.open(fpath, 'r:gz') as tar:
                        try:
                            meta_file = tar.extractfile('metadata.json')
                            if meta_file:
                                meta_data = json.loads(meta_file.read().decode('utf-8'))
                                sys_version = meta_data.get('system_version', sys_version)
                                created_at = meta_data.get('created_at', created_at)
                                checksum = meta_data.get('checksum_sha256', '')
                        except Exception:
                            pass
                except Exception:
                    pass
            elif fname.endswith('.zip'):
                try:
                    with zipfile.ZipFile(fpath, 'r') as zf:
                        if 'metadata.json' in zf.namelist():
                            meta_data = json.loads(zf.read('metadata.json').decode('utf-8'))
                            sys_version = meta_data.get('system_version', sys_version)
                            created_at = meta_data.get('created_at', created_at)
                            checksum = meta_data.get('checksum_sha256', '')
                except Exception:
                    pass
                    
            if not checksum:
                try:
                    checksum = get_file_sha256(fpath)
                except Exception:
                    checksum = ""
                    
            execute_write('''
                INSERT INTO wisp_backups (
                    filename, file_path, file_size_bytes, file_size_mb,
                    system_version, checksum_sha256, backup_type, created_by, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'manual', 'system', 'Discovered file', ?)
            ''', (fname, fpath, size_bytes, size_mb, sys_version, checksum, created_at))

def list_backups():
    """List all available backups with sequential numbering and formatted metadata."""
    sync_disk_backups()
    b_dir = get_backup_dir()
    
    rows = query_all("""
        SELECT * FROM wisp_backups
        ORDER BY created_at DESC, id DESC
    """)
    
    backups = []
    total = len(rows)
    for idx, r in enumerate(rows):
        seq_id = total - idx
        fpath = r.get('file_path') or os.path.join(b_dir, r['filename'])
        exists_on_disk = os.path.isfile(fpath)
        
        size_bytes = r.get('file_size_bytes') or (os.path.getsize(fpath) if exists_on_disk else 0)
        size_mb = r.get('file_size_mb') or round(size_bytes / (1024 * 1024), 2)
        size_str = f"{size_mb:.2f} MB" if size_mb >= 0.05 else f"{max(1, round(size_bytes / 1024))} KB"
        
        backups.append({
            'id': r['id'],
            'seq_id': seq_id,
            'filename': r['filename'],
            'file_path': fpath,
            'file_size_bytes': size_bytes,
            'file_size_mb': size_mb,
            'file_size_str': size_str,
            'system_version': r.get('system_version') or CURRENT_SYSTEM_VERSION,
            'checksum_sha256': r.get('checksum_sha256') or '',
            'created_at': r.get('created_at'),
            'created_by': r.get('created_by') or 'admin',
            'notes': r.get('notes') or '',
            'exists_on_disk': exists_on_disk
        })
        
    return backups

def generate_mysql_dump():
    """Exports all MySQL tables as a valid SQL script."""
    sql_lines = [
        "-- MAX RADIUS MySQL Database Dump",
        f"-- Generated at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "SET FOREIGN_KEY_CHECKS = 0;\n"
    ]
    
    tables = query_all("SHOW TABLES")
    table_names = []
    for t in tables:
        tname = list(t.values())[0]
        table_names.append(tname)
        
        create_res = query_one(f"SHOW CREATE TABLE `{tname}`")
        if create_res:
            create_sql = create_res.get('Create Table') or list(create_res.values())[1]
            sql_lines.append(f"DROP TABLE IF EXISTS `{tname}`;")
            sql_lines.append(f"{create_sql};\n")
            
        rows = query_all(f"SELECT * FROM `{tname}`")
        for row in rows:
            cols = ", ".join([f"`{k}`" for k in row.keys()])
            vals = []
            for v in row.values():
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    escaped = str(v).replace("\\", "\\\\").replace("'", "\\'")
                    vals.append(f"'{escaped}'")
            vals_str = ", ".join(vals)
            sql_lines.append(f"INSERT INTO `{tname}` ({cols}) VALUES ({vals_str});")
        sql_lines.append("")
        
    sql_lines.append("SET FOREIGN_KEY_CHECKS = 1;")
    return "\n".join(sql_lines), table_names

def create_backup(admin_username='admin', notes='نسخة يدوية'):
    """
    Generate a full comprehensive backup containing:
    1. Full MySQL / MariaDB database dump
    2. Uploads folder (static/uploads & storage/uploads with network logo, etc.)
    3. Card designs, custom templates, and system settings JSON
    4. Compressed in a single TAR.GZ archive file
    """
    b_dir = ensure_backup_dir()
    
    now = datetime.datetime.now()
    timestamp_str = now.strftime('%Y%m%d_%H%M%S')
    rand_suffix = secrets.randbelow(1000)
    filename = f"max_radius_backup_{timestamp_str}_{rand_suffix:03d}.tar.gz"
    filepath = os.path.join(b_dir, filename)
    
    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    conn.close()
    
    if is_mysql:
        sql_content, table_names = generate_mysql_dump()
        db_engine_name = 'mysql'
    else:
        # SQLite dump
        sql_buffer = io.StringIO()
        conn = get_connection()
        try:
            for line in conn.iterdump():
                sql_buffer.write(f"{line}\n")
        finally:
            conn.close()
        sql_content = sql_buffer.getvalue()
        tables = query_all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        table_names = [t['name'] for t in tables]
        db_engine_name = 'sqlite3'
        
    # Export system settings & card templates to JSON
    settings_rows = query_all("SELECT `key`, `value`, `description` FROM wisp_system_settings")
    card_templates = query_all("SELECT * FROM wisp_card_templates")
    
    # Metadata dictionary
    meta = {
        'filename': filename,
        'created_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'system_version': CURRENT_SYSTEM_VERSION,
        'app_name': 'MAX RADIUS',
        'db_engine': db_engine_name,
        'table_count': len(table_names),
        'tables': table_names,
        'created_by': admin_username,
        'notes': notes,
        'has_uploads': True,
        'has_card_templates': len(card_templates) > 0
    }
    
    # Pack into TAR.GZ
    with tarfile.open(filepath, 'w:gz') as tar:
        # 1. Database dump
        sql_bytes = sql_content.encode('utf-8')
        ti_sql = tarfile.TarInfo(name='database_dump.sql')
        ti_sql.size = len(sql_bytes)
        ti_sql.mtime = int(now.timestamp())
        tar.addfile(ti_sql, io.BytesIO(sql_bytes))
        
        # 2. Metadata JSON
        meta_bytes = json.dumps(meta, default=str, ensure_ascii=False, indent=2).encode('utf-8')
        ti_meta = tarfile.TarInfo(name='metadata.json')
        ti_meta.size = len(meta_bytes)
        ti_meta.mtime = int(now.timestamp())
        tar.addfile(ti_meta, io.BytesIO(meta_bytes))
        
        # 3. System Settings JSON
        config_export = {
            'settings': settings_rows,
            'card_templates': card_templates,
            'exported_at': now.strftime('%Y-%m-%d %H:%M:%S')
        }
        cfg_bytes = json.dumps(config_export, default=str, ensure_ascii=False, indent=2).encode('utf-8')
        ti_cfg = tarfile.TarInfo(name='system_config.json')
        ti_cfg.size = len(cfg_bytes)
        ti_cfg.mtime = int(now.timestamp())
        tar.addfile(ti_cfg, io.BytesIO(cfg_bytes))
        
        # 4. Uploads Directory (Logo & Attachments)
        uploads_sources = [
            str(UPLOADS_DIR),
            os.path.join(str(STORAGE_DIR), 'uploads')
        ]
        if os.name != 'nt':
            uploads_sources.extend(['/app/web/static/uploads', '/app/storage/uploads'])
        
        added_files = set()
        for u_dir in uploads_sources:
            if os.path.exists(u_dir) and os.path.isdir(u_dir):
                for root, _, files in os.walk(u_dir):
                    for file in files:
                        full_fpath = os.path.join(root, file)
                        rel_name = os.path.relpath(full_fpath, u_dir).replace('\\', '/')
                        arc_name = f"uploads/{rel_name}"
                        if arc_name not in added_files and os.path.isfile(full_fpath):
                            added_files.add(arc_name)
                            try:
                                tar.add(full_fpath, arcname=arc_name)
                            except Exception:
                                pass
                                
        # 5. SQLite raw DB file if SQLite
        if not is_mysql and os.path.exists(DB_PATH):
            try:
                tar.add(DB_PATH, arcname='radius_wisp_raw.db')
            except Exception:
                pass
                
    # Compute size and hash
    size_bytes = os.path.getsize(filepath)
    size_mb = round(size_bytes / (1024 * 1024), 2)
    checksum = get_file_sha256(filepath)
    
    # Save in DB
    existing = query_one("SELECT id FROM wisp_backups WHERE filename = ?", (filename,))
    if existing:
        execute_update('''
            UPDATE wisp_backups SET
                file_path = ?, file_size_bytes = ?, file_size_mb = ?,
                checksum_sha256 = ?, system_version = ?, notes = ?
            WHERE filename = ?
        ''', (filepath, size_bytes, size_mb, checksum, CURRENT_SYSTEM_VERSION, notes, filename))
    else:
        execute_write('''
            INSERT INTO wisp_backups (
                filename, file_path, file_size_bytes, file_size_mb,
                system_version, checksum_sha256, backup_type, created_by, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'full', ?, ?, ?)
        ''', (
            filename, filepath, size_bytes, size_mb,
            CURRENT_SYSTEM_VERSION, checksum, admin_username, notes,
            now.strftime('%Y-%m-%d %H:%M:%S')
        ))
        
    # Update last run in settings
    try:
        execute_update("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = 'backup_last_run'", (now.strftime('%Y-%m-%d %H:%M:%S'),))
        execute_update("UPDATE wisp_system_settings SET `value` = 'success' WHERE `key` = 'backup_last_status'", ())
        execute_update("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = 'backup_last_message'", (f"Created backup {filename} ({size_mb} MB)",))
    except Exception:
        pass
        
    log_audit(1, admin_username, 'CREATE_BACKUP', 'backup', f'Created comprehensive backup {filename} ({size_mb} MB)')
    return True, f"تم إنشاء النسخة الاحتياطية الشاملة [{filename}] بنجاح بحجم ({size_mb:.2f} MB).", {
        'filename': filename,
        'file_size_mb': size_mb,
        'system_version': CURRENT_SYSTEM_VERSION
    }

def upload_backup_file(file_storage, admin_username='admin'):
    """Save an uploaded backup file and record it."""
    b_dir = ensure_backup_dir()
    if not file_storage or not file_storage.filename:
        return False, "يرجى اختيار ملف صالح لرفعه."
        
    orig_filename = file_storage.filename.strip()
    ext = os.path.splitext(orig_filename)[1].lower()
    if not (orig_filename.endswith('.tar.gz') or orig_filename.endswith('.tgz') or ext in ['.zip', '.sql', '.gz', '.db']):
        return False, "صيغة الملف غير مدعومة. يرجى رفع ملف بصيغة (.tar.gz أو .zip أو .sql أو .db)."
        
    clean_name = os.path.basename(orig_filename).replace(' ', '_')
    if not clean_name.startswith('max_radius_') and not clean_name.startswith('wisp_') and not clean_name.startswith('backup_'):
        clean_name = f"uploaded_{clean_name}"
        
    target_path = os.path.join(b_dir, clean_name)
    counter = 1
    base_stem, base_ext = os.path.splitext(clean_name)
    if clean_name.endswith('.tar.gz'):
        base_stem = clean_name[:-7]
        base_ext = '.tar.gz'
    while os.path.exists(target_path):
        clean_name = f"{base_stem}_{counter}{base_ext}"
        target_path = os.path.join(b_dir, clean_name)
        counter += 1
        
    file_storage.save(target_path)
    size_bytes = os.path.getsize(target_path)
    if size_bytes == 0:
        if os.path.exists(target_path): os.remove(target_path)
        return False, "الملف المرفوع فارغ (0 بايت)."
        
    size_mb = round(size_bytes / (1024 * 1024), 2)
    sys_version = CURRENT_SYSTEM_VERSION
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    checksum = get_file_sha256(target_path)
    
    execute_write('''
        INSERT INTO wisp_backups (
            filename, file_path, file_size_bytes, file_size_mb,
            system_version, checksum_sha256, backup_type, created_by, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'uploaded', ?, 'Manual upload', ?)
    ''', (clean_name, target_path, size_bytes, size_mb, sys_version, checksum, admin_username, now_str))
    
    log_audit(1, admin_username, 'UPLOAD_BACKUP', 'backup', f'Uploaded backup {clean_name} ({size_mb} MB)')
    return True, f"تم رفع النسخة الاحتياطية [{clean_name}] بنجاح بحجم ({size_mb} MB)."

def restore_backup(filename, admin_username='admin'):
    """
    Comprehensive restore:
    1. Extracts database SQL dump and updates MariaDB/MySQL.
    2. Restores uploads directory (logos, images) to static/uploads & storage/uploads.
    3. Restores system configs and card templates if present.
    """
    b_dir = ensure_backup_dir()
    safe_name = os.path.basename(filename)
    filepath = os.path.join(b_dir, safe_name)
    if not os.path.isfile(filepath):
        return False, f"ملف النسخة الاحتياطية [{safe_name}] غير موجود على القرص."
        
    try:
        sql_content = ""
        is_tar = safe_name.endswith('.tar.gz') or safe_name.endswith('.tgz')
        is_zip = safe_name.endswith('.zip')
        is_sql = safe_name.endswith('.sql')
        
        target_uploads_dirs = [
            str(UPLOADS_DIR),
            os.path.join(str(STORAGE_DIR), 'uploads')
        ]
        if os.name != 'nt':
            target_uploads_dirs.extend(['/app/web/static/uploads', '/app/storage/uploads'])
        for ud in target_uploads_dirs:
            os.makedirs(ud, exist_ok=True)
            
        restored_files_count = 0
        
        # 1. Extract from TAR.GZ
        if is_tar:
            with tarfile.open(filepath, 'r:gz') as tar:
                for member in tar.getmembers():
                    if member.name == 'database_dump.sql':
                        f = tar.extractfile(member)
                        if f:
                            sql_content = f.read().decode('utf-8', errors='replace')
                    elif member.name.startswith('uploads/') and not member.isdir():
                        rel_file = member.name[len('uploads/'):]
                        f = tar.extractfile(member)
                        if f:
                            data = f.read()
                            for ud in target_uploads_dirs:
                                dest_path = os.path.join(ud, rel_file)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                with open(dest_path, 'wb') as out_f:
                                    out_f.write(data)
                            restored_files_count += 1
                    elif member.name == 'system_config.json':
                        # Config JSON backup
                        pass
                        
        # 2. Extract from ZIP
        elif is_zip:
            with zipfile.ZipFile(filepath, 'r') as zf:
                namelist = zf.namelist()
                if 'database_dump.sql' in namelist:
                    sql_content = zf.read('database_dump.sql').decode('utf-8', errors='replace')
                elif 'radius_wisp_raw.db' in namelist and not is_mysql_conn(get_connection()):
                    raw_bytes = zf.read('radius_wisp_raw.db')
                    with open(DB_PATH, 'wb') as f:
                        f.write(raw_bytes)
                    return True, f"تمت استعادة النسخة الاحتياطية [{safe_name}] بنجاح."
                    
                # Check for uploads inside ZIP
                for name in namelist:
                    if name.startswith('uploads/') and not name.endswith('/'):
                        rel_file = name[len('uploads/'):]
                        data = zf.read(name)
                        for ud in target_uploads_dirs:
                            dest_path = os.path.join(ud, rel_file)
                            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                            with open(dest_path, 'wb') as out_f:
                                out_f.write(data)
                        restored_files_count += 1
                        
        # 3. Plain SQL
        elif is_sql:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                sql_content = f.read()
                
        # Execute database restoration
        if sql_content:
            conn = get_connection()
            is_mysql = is_mysql_conn(conn)
            conn.close()
            
            if is_mysql:
                commands = split_sql_statements(sql_content)
                with db_session() as c:
                    cursor = c.cursor()
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
                    for cmd in commands:
                        cleaned = cmd.strip()
                        if cleaned and not cleaned.startswith('--') and not cleaned.startswith('/*') and not cleaned.startswith('#'):
                            cursor.execute(cleaned)
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
            else:
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    cursor.execute("PRAGMA foreign_keys = OFF;")
                    tbl_rows = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                    for row in tbl_rows:
                        cursor.execute(f'DROP TABLE IF EXISTS "{row[0]}";')
                    conn.commit()
                    conn.executescript(sql_content)
                    conn.commit()
                finally:
                    conn.close()
                    
        # Self-heal schema immediately after restoration
        try:
            from database.schema_healer import heal_database_schema
            heal_database_schema()
        except Exception as e:
            print(f"[Schema Healer Restore Notice]: {e}")

        # Layer 1: Cleanly sanitize restored open sessions
        try:
            execute_write("""
                UPDATE radacct 
                SET acctstoptime = COALESCE(acctupdatetime, acctstarttime, CURRENT_TIMESTAMP),
                    acctterminatecause = 'Backup-Restored-Closed'
                WHERE acctstoptime IS NULL
            """)
            print("[Restore Hook] Successfully sanitized and closed restored ghost sessions in radacct.")
        except Exception as e_hook:
            print(f"[Restore Hook Warning]: {e_hook}")

        extra_info = f" واستعادة {restored_files_count} ملف مرفقات/شعارات" if restored_files_count > 0 else ""
        log_audit(1, admin_username, 'RESTORE_BACKUP', 'backup', f'Restored database & files from {safe_name}')
        return True, f"تمت استعادة النسخة الاحتياطية [{safe_name}] بنجاح وتحديث قاعدة البيانات وتطبيق المعالجة الذاتية بالكامل{extra_info}."

    except Exception as err:
        return False, f"حدث خطأ أثناء استعادة النسخة الاحتياطية: {str(err)}"

def delete_backup(filename, admin_username='admin'):
    """Delete a backup file from storage and database."""
    b_dir = ensure_backup_dir()
    safe_name = os.path.basename(filename)
    filepath = os.path.join(b_dir, safe_name)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            return False, f"فشل حذف الملف من القرص: {str(e)}"
            
    execute_write("DELETE FROM wisp_backups WHERE filename = ?", (safe_name,))
    log_audit(1, admin_username, 'DELETE_BACKUP', 'backup', f'Deleted backup file {safe_name}')
    return True, f"تم حذف ملف النسخة الاحتياطية [{safe_name}] نهائياً بنجاح."

def get_backup_filepath(filename):
    """Get verified file path for download."""
    b_dir = ensure_backup_dir()
    safe_name = os.path.basename(filename)
    filepath = os.path.join(b_dir, safe_name)
    if os.path.isfile(filepath):
        return filepath
    return None