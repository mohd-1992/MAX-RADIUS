# -*- coding: utf-8 -*-
"""
Database Access Layer for WISP FreeRADIUS & MikroTik Manager.
Supports MySQL / MariaDB (Primary) with SQLite fallback and dynamic query parameter adaptation.
"""

import os
import re
import datetime
from contextlib import contextmanager

from core.config import (
    DB_TYPE, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
    SQLITE_DB_PATH, DB_PATH
)

try:
    import pymysql
    from pymysql.cursors import DictCursor
    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False

try:
    import sqlite3
    SQLITE_AVAILABLE = True
except ImportError:
    SQLITE_AVAILABLE = False

def is_mysql_conn(conn):
    """Returns True if connection is not a standard SQLite connection."""
    if SQLITE_AVAILABLE and isinstance(conn, sqlite3.Connection):
        return False
    return True

def get_connection():
    """Returns a database connection based on configured DB_TYPE or fallback."""
    if DB_TYPE == 'mysql' and PYMYSQL_AVAILABLE:
        try:
            conn = pymysql.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset='utf8mb4',
                cursorclass=DictCursor,
                autocommit=False,
                connect_timeout=5
            )
            return conn
        except Exception:
            # Fallback to SQLite if MySQL is unreachable
            pass
            
    if SQLITE_AVAILABLE:
        conn = sqlite3.connect(SQLITE_DB_PATH, timeout=25.0, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON;')
        conn.execute('PRAGMA journal_mode = WAL;')
        conn.execute('PRAGMA synchronous = NORMAL;')
        return conn
    else:
        raise RuntimeError("No database driver available.")

@contextmanager
def db_session():
    """Transaction context manager."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def adapt_query(query, conn):
    """Adapts query parameters placeholder based on the active connection type."""
    if is_mysql_conn(conn):
        return query.replace('?', '%s')
    return query

def _is_schema_error(err):
    err_str = str(err).lower()
    if '1054' in err_str or 'unknown column' in err_str or '1146' in err_str or "doesn't exist" in err_str or 'no such column' in err_str or 'no such table' in err_str:
        return True
    if hasattr(err, 'args') and len(err.args) > 0 and err.args[0] in (1054, 1146):
        return True
    return False

def _run_with_autoheal(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if _is_schema_error(e):
            print(f"[DB Auto-Heal Triggered] Schema mismatch detected: {e}. Running self-healing engine...")
            try:
                from database.schema_healer import heal_database_schema
                heal_database_schema()
                return func(*args, **kwargs)
            except Exception:
                pass
        raise e

def _raw_query_all(query, params=()):
    with db_session() as conn:
        sql = adapt_query(query, conn)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        if not is_mysql_conn(conn):
            return [dict(row) for row in rows]
        return list(rows) if rows else []

def query_all(query, params=()):
    return _run_with_autoheal(_raw_query_all, query, params)

def _raw_query_one(query, params=()):
    with db_session() as conn:
        sql = adapt_query(query, conn)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

def query_one(query, params=()):
    return _run_with_autoheal(_raw_query_one, query, params)

def _raw_execute_write(query, params=()):
    with db_session() as conn:
        sql = adapt_query(query, conn)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.lastrowid

def execute_write(query, params=()):
    return _run_with_autoheal(_raw_execute_write, query, params)

def _raw_execute_update(query, params=()):
    with db_session() as conn:
        sql = adapt_query(query, conn)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return cursor.rowcount

def execute_update(query, params=()):
    return _run_with_autoheal(_raw_execute_update, query, params)

def _raw_execute_many(query, params_list):
    with db_session() as conn:
        sql = adapt_query(query, conn)
        cursor = conn.cursor()
        cursor.executemany(sql, params_list)
        return cursor.rowcount

def execute_many(query, params_list):
    return _run_with_autoheal(_raw_execute_many, query, params_list)

def log_audit(admin_id, username, action, module, details=None, ip_address=None):
    try:
        execute_write('''
            INSERT INTO wisp_audit_logs (admin_id, username, action, module, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (admin_id or 1, username or 'admin', action, module, str(details or ''), ip_address or '127.0.0.1'))
    except Exception as e:
        print(f"[Audit Log Error]: {e}")

def log_user_audit(user_type, user_id, username, admin_name, action, change_details):
    """
    Logs administrative profile updates and actions on a specific subscriber or voucher card.
    """
    try:
        execute_write('''
            INSERT INTO user_audit_logs (user_type, user_id, username, admin_name, action, change_details)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_type, user_id, username, admin_name or 'Admin', action or 'UPDATE_PROFILE', str(change_details or '')))
    except Exception as e:
        print(f"[User Audit Log Error]: {e}")

def init_database():
    """Executes schema initialization script on first startup."""
    current_dir = os.path.dirname(__file__)
    
    if DB_TYPE == 'mysql' and PYMYSQL_AVAILABLE:
        schema_file = os.path.join(current_dir, 'schema_mysql.sql')
        if not os.path.exists(schema_file):
            return
            
        try:
            conn = get_connection()
            if is_mysql_conn(conn):
                # Check if core tables exist
                has_subs = False
                try:
                    with conn.cursor() as cur:
                        cur.execute("SHOW TABLES LIKE 'wisp_subscribers'")
                        has_subs = bool(cur.fetchone())
                except Exception:
                    pass
                    
                if not has_subs:
                    print(f"[DB] Core tables missing in ({DB_HOST}:{DB_PORT}/{DB_NAME}). Auto-importing full schema...")
                    with open(schema_file, 'r', encoding='utf-8') as f:
                        sql_content = f.read()

                    clean_lines = []
                    delim = ';'
                    for line in sql_content.splitlines():
                        sline = line.strip()
                        if sline.upper().startswith('DELIMITER'):
                            delim = sline.split()[1] if len(sline.split()) > 1 else ';'
                            continue
                        if delim != ';' and sline.endswith(delim):
                            continue
                        if delim == ';' and not sline.startswith('--'):
                            clean_lines.append(line)
                            
                    full_clean_sql = '\n'.join(clean_lines)
                    commands = [c.strip() for c in full_clean_sql.split(';') if c.strip()]
                    
                    for cmd in commands:
                        if cmd and not cmd.startswith('--'):
                            try:
                                with conn.cursor() as cur:
                                    cur.execute(cmd)
                                conn.commit()
                            except Exception:
                                pass
                    print("[DB] MySQL FreeRADIUS & WISP Tables and Indexes initialized successfully.")
                conn.close()
                
                try:
                    from database.schema_healer import heal_database_schema
                    heal_database_schema()
                except Exception as e:
                    print(f"[Schema Healer Auto-Run Notice]: {e}")
                return
        except Exception as e:
            print(f"[DB Notice]: MySQL schema check: {e}")
            
    if SQLITE_AVAILABLE and (DB_TYPE != 'mysql' or not PYMYSQL_AVAILABLE):
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            for sql_file in ['freeradius_standard.sql', 'wisp_extensions.sql', 'seed_data.sql']:
                path = os.path.join(current_dir, sql_file)
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        cursor.executescript(f.read())
            conn.commit()
            print("[DB] SQLite Fallback Database initialized successfully.")
        finally:
            conn.close()