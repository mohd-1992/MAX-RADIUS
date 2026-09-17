# -*- coding: utf-8 -*-
"""
services/traffic_analytics_service.py
-------------------------------------
Network Traffic & Bandwidth Analytics Inspector for MAX RADIUS.
Calculates real-time bandwidth consumption, top data hogs, hourly peak usage distribution, and anomalous sessions.
"""

from datetime import datetime, timedelta
from database.db import get_connection

_get_db = get_connection

def get_traffic_analytics_report(timeframe='30d'):
    """Generate comprehensive traffic metrics, dual download/upload curves, and peak distribution."""
    db = _get_db()
    cur = db.cursor()
    try:
        # Build timeframe condition
        if timeframe == 'today':
            date_filter = "acctstarttime >= CURDATE()"
            prev_filter = "acctstarttime >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND acctstarttime < CURDATE()"
        elif timeframe == '7d':
            date_filter = "acctstarttime >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
            prev_filter = "acctstarttime >= DATE_SUB(NOW(), INTERVAL 14 DAY) AND acctstarttime < DATE_SUB(NOW(), INTERVAL 7 DAY)"
        else: # 30d default
            date_filter = "acctstarttime >= DATE_SUB(NOW(), INTERVAL 30 DAY)"
            prev_filter = "acctstarttime >= DATE_SUB(NOW(), INTERVAL 60 DAY) AND acctstarttime < DATE_SUB(NOW(), INTERVAL 30 DAY)"

        # 1. Total Cumulative Download & Upload
        cur.execute(f"""
            SELECT 
                COALESCE(SUM(acctinputoctets), 0) as total_upload_bytes,
                COALESCE(SUM(acctoutputoctets), 0) as total_download_bytes,
                COUNT(*) as total_sessions,
                COUNT(DISTINCT username) as total_unique_users
            FROM radacct
            WHERE {date_filter}
        """)
        overall = cur.fetchone() or {}
        if not isinstance(overall, dict) and hasattr(overall, 'keys'):
            overall = dict(overall)

        total_upload_bytes = float(overall.get('total_upload_bytes') or 0)
        total_download_bytes = float(overall.get('total_download_bytes') or 0)
        total_traffic_bytes = total_upload_bytes + total_download_bytes

        total_upload_gb = round(total_upload_bytes / (1024 ** 3), 2)
        total_download_gb = round(total_download_bytes / (1024 ** 3), 2)
        total_traffic_gb = round(total_traffic_bytes / (1024 ** 3), 2)

        total_upload_mb = round(total_upload_bytes / (1024 ** 2), 2)
        total_download_mb = round(total_download_bytes / (1024 ** 2), 2)
        total_traffic_mb = round(total_traffic_bytes / (1024 ** 2), 2)

        download_ratio = round((total_download_bytes / total_traffic_bytes * 100) if total_traffic_bytes > 0 else 85.0, 1)
        upload_ratio = round(100.0 - download_ratio, 1)

        # 2. Top 10 Consumers (Data Hogs)
        cur.execute(f"""
            SELECT 
                username,
                COUNT(*) as session_count,
                SUM(acctinputoctets) as up_bytes,
                SUM(acctoutputoctets) as down_bytes,
                SUM(acctinputoctets + acctoutputoctets) as total_bytes,
                SUM(acctsessiontime) as total_duration_sec,
                MAX(acctstarttime) as last_seen
            FROM radacct
            WHERE {date_filter}
            GROUP BY username
            ORDER BY total_bytes DESC
            LIMIT 10
        """)
        raw_top_users = cur.fetchall() or []

        top_users = []
        for u in raw_top_users:
            if not isinstance(u, dict) and hasattr(u, 'keys'):
                u = dict(u)
            t_bytes = float(u['total_bytes'] or 0)
            t_gb = round(t_bytes / (1024 ** 3), 2)
            t_mb = round(t_bytes / (1024 ** 2), 1)
            hours = round((u['total_duration_sec'] or 0) / 3600, 1)
            user_percent = round((t_bytes / total_traffic_bytes * 100) if total_traffic_bytes > 0 else 0.0, 1)
            top_users.append({
                'username': u['username'],
                'total_gb': t_gb,
                'total_mb': t_mb,
                'upload_mb': round(float(u['up_bytes'] or 0) / (1024 ** 2), 1),
                'download_mb': round(float(u['down_bytes'] or 0) / (1024 ** 2), 1),
                'session_count': u['session_count'],
                'total_hours': hours,
                'last_seen': str(u['last_seen']),
                'share_percent': user_percent
            })

        # 3. Peak Hour Distribution (24-Hour traffic distribution with separate DL and UL)
        cur.execute(f"""
            SELECT 
                HOUR(acctstarttime) as hour_of_day,
                COUNT(*) as sessions_count,
                SUM(acctinputoctets) as hour_up_bytes,
                SUM(acctoutputoctets) as hour_down_bytes,
                SUM(acctinputoctets + acctoutputoctets) as hour_traffic_bytes
            FROM radacct
            WHERE {date_filter}
            GROUP BY HOUR(acctstarttime)
            ORDER BY hour_of_day ASC
        """)
        raw_hourly = cur.fetchall() or []
        hourly_raw = {}
        for r in raw_hourly:
            if not isinstance(r, dict) and hasattr(r, 'keys'):
                r = dict(r)
            if 'hour_of_day' in r:
                hourly_raw[r['hour_of_day']] = r

        hourly_distribution = []
        morning_traffic = 0.0
        afternoon_traffic = 0.0
        evening_traffic = 0.0
        night_traffic = 0.0

        chart_labels = []
        chart_download_mb = []
        chart_upload_mb = []
        chart_total_mb = []
        chart_sessions = []

        for h in range(24):
            item = hourly_raw.get(h, {})
            up_b = float(item.get('hour_up_bytes') or 0)
            down_b = float(item.get('hour_down_bytes') or 0)
            t_bytes = up_b + down_b

            dl_mb = round(down_b / (1024 ** 2), 2)
            ul_mb = round(up_b / (1024 ** 2), 2)
            tot_mb = round(t_bytes / (1024 ** 2), 2)
            sess = item.get('sessions_count', 0)

            hour_label = f"{h:02d}:00"
            chart_labels.append(hour_label)
            chart_download_mb.append(dl_mb)
            chart_upload_mb.append(ul_mb)
            chart_total_mb.append(tot_mb)
            chart_sessions.append(sess)

            # Time blocks
            if 6 <= h < 12:
                morning_traffic += tot_mb
            elif 12 <= h < 18:
                afternoon_traffic += tot_mb
            elif 18 <= h <= 23:
                evening_traffic += tot_mb
            else:
                night_traffic += tot_mb

            hourly_distribution.append({
                'hour': hour_label,
                'download_mb': dl_mb,
                'upload_mb': ul_mb,
                'traffic_mb': tot_mb,
                'download_gb': round(dl_mb / 1024, 3),
                'upload_gb': round(ul_mb / 1024, 3),
                'traffic_gb': round(tot_mb / 1024, 3),
                'sessions': sess
            })

        # Peak Hour & Off-Peak Hour
        sorted_by_traffic = sorted(hourly_distribution, key=lambda x: x['traffic_mb'], reverse=True)
        peak_hour = sorted_by_traffic[0]['hour'] if sorted_by_traffic else "20:00"
        peak_hour_traffic_mb = sorted_by_traffic[0]['traffic_mb'] if sorted_by_traffic else 0.0

        sorted_by_traffic_asc = sorted(hourly_distribution, key=lambda x: x['traffic_mb'])
        offpeak_hour = sorted_by_traffic_asc[0]['hour'] if sorted_by_traffic_asc else "04:00"

        # 4. Anomalous Mega Sessions
        cur.execute(f"""
            SELECT 
                radacctid,
                username,
                nasipaddress,
                framedipaddress,
                acctstarttime,
                acctstoptime,
                acctsessiontime,
                (acctinputoctets + acctoutputoctets) as session_bytes
            FROM radacct
            WHERE {date_filter}
              AND ((acctinputoctets + acctoutputoctets) > 1073741824
                   OR acctsessiontime > 86400)
            ORDER BY session_bytes DESC
            LIMIT 10
        """)
        raw_anomalies = cur.fetchall() or []
        anomalies = []
        for a in raw_anomalies:
            if not isinstance(a, dict) and hasattr(a, 'keys'):
                a = dict(a)
            anomalies.append({
                'id': a['radacctid'],
                'username': a['username'],
                'nas': a['nasipaddress'],
                'ip': a['framedipaddress'],
                'start': str(a['acctstarttime']),
                'stop': str(a['acctstoptime']) if a['acctstoptime'] else 'جلسة حية مفتوحة',
                'size_gb': round(float(a['session_bytes']) / (1024 ** 3), 2),
                'duration_hours': round((a['acctsessiontime'] or 0) / 3600, 1)
            })

        return {
            'timeframe': timeframe,
            'total_download_gb': total_download_gb,
            'total_upload_gb': total_upload_gb,
            'total_traffic_gb': total_traffic_gb,
            'total_download_mb': total_download_mb,
            'total_upload_mb': total_upload_mb,
            'total_traffic_mb': total_traffic_mb,
            'download_ratio': download_ratio,
            'upload_ratio': upload_ratio,
            'total_sessions': overall.get('total_sessions', 0),
            'unique_users': overall.get('total_unique_users', 0),
            'peak_hour': peak_hour,
            'peak_hour_traffic_mb': peak_hour_traffic_mb,
            'offpeak_hour': offpeak_hour,
            'top_users': top_users,
            'hourly_distribution': hourly_distribution,
            'chart_data': {
                'labels': chart_labels,
                'download_mb': chart_download_mb,
                'upload_mb': chart_upload_mb,
                'total_mb': chart_total_mb,
                'sessions': chart_sessions
            },
            'time_blocks': {
                'morning_mb': round(morning_traffic, 1),
                'afternoon_mb': round(afternoon_traffic, 1),
                'evening_mb': round(evening_traffic, 1),
                'night_mb': round(night_traffic, 1)
            },
            'anomalies': anomalies
        }
    finally:
        try:
            cur.close()
        except Exception:
            pass
        db.close()
