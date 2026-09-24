# -*- coding: utf-8 -*-
"""
Central Configuration Module for MAX RADIUS & MikroTik Manager (MySQL Edition).
"""

import os
from pathlib import Path

# Application Identity & Centralized Versioning System
APP_NAME = os.environ.get('APP_NAME', 'MAX RADIUS').replace('_', ' ')
APP_VERSION = "2.6.2"
APP_EDITION = os.environ.get('APP_EDITION', 'Enterprise')
APP_VERSION_FULL = f"{APP_NAME} v{APP_VERSION}"
APP_VERSION_BADGE = f"v{APP_VERSION} {APP_EDITION}"

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent

# Application Environment & Web Port
APP_ENV = os.environ.get('APP_ENV', 'production')
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 't')
SECRET_KEY = os.environ.get('SECRET_KEY', 'max-radius-secret-key-prod-2026')
APP_HOST = os.environ.get('HOST', '0.0.0.0')
APP_PORT = int(os.environ.get('PORT', 5090))

# Storage & Volume Paths
STORAGE_DIR = Path(os.environ.get('STORAGE_DIR', BASE_DIR / 'storage'))
BACKUPS_DIR = Path(os.environ.get('BACKUPS_DIR', STORAGE_DIR / 'backups'))
UPLOADS_DIR = Path(os.environ.get('UPLOADS_DIR', STORAGE_DIR / 'uploads'))
LOGS_DIR = Path(os.environ.get('LOGS_DIR', STORAGE_DIR / 'logs'))

for folder in [STORAGE_DIR, BACKUPS_DIR, UPLOADS_DIR, LOGS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

# Database Configuration (MySQL / MariaDB / SQLite fallback)
DB_TYPE = os.environ.get('DB_TYPE', 'mysql').lower()
DB_HOST = os.environ.get('DB_HOST', '127.0.0.1')
DB_PORT = int(os.environ.get('DB_PORT', 3306))
DB_USER = os.environ.get('DB_USER', 'radius')
DB_PASSWORD = os.environ.get('DB_PASSWORD', 'radpass')
DB_NAME = os.environ.get('DB_NAME', 'radius_wisp')
SQLITE_DB_PATH = os.environ.get('SQLITE_DB_PATH', str(BASE_DIR / 'database' / 'radius_wisp.db'))
DB_PATH = SQLITE_DB_PATH

# FreeRADIUS & Network Ports (Separate testing ports by default)
RADIUS_AUTH_PORT = int(os.environ.get('RADIUS_AUTH_PORT', 18120))
RADIUS_ACCT_PORT = int(os.environ.get('RADIUS_ACCT_PORT', 18130))
RADIUS_COA_PORT = int(os.environ.get('RADIUS_COA_PORT', 37990))
RADIUS_SECRET_DEFAULT = os.environ.get('RADIUS_SECRET_DEFAULT', 'testing123')