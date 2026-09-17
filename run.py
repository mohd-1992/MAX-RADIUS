# -*- coding: utf-8 -*-
"""
Production Launcher for WISP Web Dashboard & Admin Interface.
Connects directly to MySQL (MariaDB) for subscriber/voucher management.
All RADIUS Authentication & Accounting are handled by the Official FreeRADIUS container.
"""

import os
import sys
import socket

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core.config import (
    APP_HOST, APP_PORT, DB_TYPE, DB_HOST, DB_PORT, DB_NAME
)
from database.db import init_database
from web.app import app

def get_local_lan_ip():
    """Detect the local machine's primary LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def main():
    print("=" * 72)
    print("  🚀 نظام إدارة شبكات WISP ولوحة التحكم (Web Dashboard)")
    print(f"  🗄️  قاعدة البيانات: {DB_TYPE.upper()} ({DB_HOST}:{DB_PORT}/{DB_NAME})")
    print("  📡 محرك المصادقة والمحاسبة: FreeRADIUS 3.x Official Engine Container")
    print("=" * 72)
    
    # 1. Initialize Database Schema if needed
    try:
        init_database()
    except Exception as e:
        print(f"  [!] تنبيه في فحص قاعدة البيانات: {e}")

    # 2. Start Internal Watchdog & Self-Healing Daemon
    try:
        from core.watchdog import start_watchdog_thread
        start_watchdog_thread()
    except Exception as e:
        print(f"  [!] تنبيه في تشغيل المراقب الآلي: {e}")

    # 3. Start NTP Real-Time Sync Daemon
    try:
        from core.time_service import start_ntp_sync_worker
        start_ntp_sync_worker(interval_seconds=3600)
    except Exception as e:
        print(f"  [!] تنبيه في تشغيل مزامنة NTP: {e}")

    # 4. Start Automated Backup Scheduler Engine
    try:
        from services.backup_scheduler_service import init_backup_scheduler
        init_backup_scheduler()
    except Exception as e:
        print(f"  [!] تنبيه في تشغيل مجدول النسخ الاحتياطي: {e}")

    # 5. Start Real-Time Live Metrics Daemon
    try:
        from services.stats_service import start_live_metrics_daemon
        start_live_metrics_daemon()
    except Exception as e:
        print(f"  [!] تنبيه في تشغيل مراقب المؤشرات الحية: {e}")

    # 6. Start 5-minute Real-Time License Heartbeat Sync Daemon
    try:
        from services.license_guard_service import start_license_heartbeat_daemon
        start_license_heartbeat_daemon(interval_seconds=300)
    except Exception as e:
        print(f"  [!] تنبيه في تشغيل مراقب نبضات الترخيص: {e}")

    
    port = APP_PORT
    host = APP_HOST
    lan_ip = get_local_lan_ip()
    
    print("\n  🌐 روابط لوحة التحكم (المنفذ 5090):")
    print(f"     1. من هذا الكمبيوتر (Localhost):  http://localhost:{port}")
    print(f"     2. من أي جهاز آخر في الشبكة:       http://{lan_ip}:{port}")
    print("=" * 72 + "\n")

    try:
        from waitress import serve
        print(f"[*] تشغيل خادم الإنتاج السريع Waitress WSGI على 0.0.0.0:{port}...")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print(f"[*] تشغيل خادم التطوير الداخلي Flask على 0.0.0.0:{port}...")
        app.run(host=host, port=port, debug=True)

if __name__ == '__main__':
    main()