# -*- coding: utf-8 -*-
"""
Database Access Layer Re-exports for Web Application.
Prevents circular imports between web routes, services, and core modules.
"""

from database.db import (
    init_database,
    get_connection,
    db_session,
    query_all,
    query_one,
    execute_write,
    execute_update,
    execute_many,
    log_audit,
    log_user_audit,
    DB_PATH
)

__all__ = [
    'init_database',
    'get_connection',
    'db_session',
    'query_all',
    'query_one',
    'execute_write',
    'execute_update',
    'execute_many',
    'log_audit',
    'log_user_audit',
    'DB_PATH'
]
