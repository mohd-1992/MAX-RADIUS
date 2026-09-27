#!/bin/bash
# ============================================================
# init.sh — يُنفَّذ تلقائياً عند أول بدء تشغيل MariaDB
# يستورد كامل قاعدة البيانات من السيرفر (بيانات + هيكل)
# ============================================================
set -e

DUMP_FILE="/docker-entrypoint-initdb.d/full_db.sql"

if [ -f "$DUMP_FILE" ]; then
    echo "[INIT] Importing full database dump from server..."
    mariadb -u root -p"${MYSQL_ROOT_PASSWORD}" radius_wisp < "$DUMP_FILE"
    echo "[INIT] Database import complete. All data restored."
else
    echo "[INIT] Warning: full_db.sql not found, starting with empty database."
fi
