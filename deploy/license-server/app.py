# -*- coding: utf-8 -*-
"""
Main Flask Web Application & REST API for Master License Server & Telemetry Hub
Full CRUD, cryptographic signing, live instance monitoring, remote actions, and telemetry.
"""

import os
import sys
import io
import json
import base64
import hashlib
import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, flash, session, g

import config
from database.db import (
    init_db, query_all, query_one, execute_write, log_audit,
    get_setting, set_setting, is_blacklisted
)
from core.crypto_engine import ensure_master_keypair, get_public_key_pem, verify_payload_signature
from core.license_builder import generate_license_package
from core.instance_enrollment import activation_bundle, EnrollmentError
from core.hardware_verifier import normalize_hardware_id, match_machine_id
from core.notifier import (
    send_telegram_alert, test_telegram_connection,
    notify_rogue_instance, notify_hwid_mismatch, notify_trial_issued
)
import threading
from core.backup_service import (
    create_database_backup, create_client_backup, get_backup_file, delete_backup,
    get_backup_schedule_settings, save_backup_schedule_settings, check_and_run_scheduled_backup
)


def parse_flexible_datetime(dt_val):
    """Parses various datetime string formats (ISO, standard, date-only) safely."""
    if isinstance(dt_val, datetime.datetime):
        return dt_val
    if not dt_val:
        return datetime.datetime.utcnow()
    
    dt_str = str(dt_val).strip()
    dt_clean = dt_str.replace('T', ' ').replace('Z', '').split('.')[0].strip()
    
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d"
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(dt_clean, fmt)
        except Exception:
            pass
    try:
        return datetime.datetime.fromisoformat(dt_str.replace('Z', ''))
    except Exception:
        return datetime.datetime.utcnow()


app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Session Configuration
app.config['SESSION_COOKIE_NAME'] = 'max_master_license_session'
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

# Ensure Keys & Database on startup
ensure_master_keypair()
init_db()
from core.license_security import install_csrf
from core.heartbeat_security import install_protocol, register_key, claim_installation
install_csrf(app, lambda req: req.path != '/api/v1/heartbeat/ping')
install_protocol(app)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('master_admin'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# Context processor for global template variables
@app.route('/instances/register-key', methods=['POST'])
@login_required
def register_instance_key_action():
    try:
        register_key(request.form.get('instance_uuid',''), request.form.get('public_key',''))
        log_audit('INSTANCE_KEY_REGISTERED', 'instances', request.form.get('instance_uuid',''), 'Administrator enrolled public key')
        flash('تم اعتماد مفتاح اتصال النسخة. تحقق من مطابقته للمفتاح المعروض داخل النظام.', 'success')
    except ValueError:
        flash('معرف النسخة أو مفتاح الاتصال غير صالح.', 'danger')
    return redirect(url_for('instances_list'))


# Context processor for global template variables
@app.context_processor
def inject_global_vars():
    active_lic_count = 0
    instances_online_count = 0
    unlicensed_count = 0
    clients_count = 0
    blacklist_count = 0
    updates_count = 0
    try:
        ac = query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'active'")
        active_lic_count = ac['c'] if ac else 0
        
        since_10m = (datetime.datetime.utcnow() - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        oc = query_one("SELECT COUNT(*) as c FROM master_instances WHERE last_ping_at >= ?", (since_10m,))
        instances_online_count = oc['c'] if oc else 0
        
        uc = query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'unlicensed'")
        unlicensed_count = uc['c'] if uc else 0
        
        cc = query_one("SELECT COUNT(*) as c FROM master_clients")
        clients_count = cc['c'] if cc else 0
        
        bc = query_one("SELECT COUNT(*) as c FROM master_blacklist")
        blacklist_count = bc['c'] if bc else 0
        
        upc = query_one("SELECT COUNT(*) as c FROM master_updates")
        updates_count = upc['c'] if upc else 0
    except Exception:
        pass
        
    return {
        'system_name': config.SYSTEM_NAME,
        'system_version': config.SYSTEM_VERSION,
        'global_active_licenses_count': active_lic_count,
        'global_instances_online_count': instances_online_count,
        'global_unlicensed_count': unlicensed_count,
        'global_clients_count': clients_count,
        'global_blacklist_count': blacklist_count,
        'global_updates_count': updates_count,
        'current_admin': session.get('master_admin', 'admin'),
        'current_admin_name': session.get('admin_name', 'المدير العام')
    }


def parse_server_binding_input(binding_str, default_hw='ANY', default_inst='ANY'):
    """
    Intelligently parses combined or individual server binding input into (hardware_id, instance_id).
    Supports:
    - Combined: "MAX-XXXX-XXXX-XXXX-XXXX / INST-XXXXXXXX" -> (HWID, INST)
    - Combined with pipe/comma: "MAX-XXXX... | INST-..."
    - Only HWID: "MAX-XXXX-XXXX-XXXX-XXXX" -> (HWID, default_inst)
    - Only Instance UUID: "INST-XXXXXXXX" -> (default_hw, INST)
    - "ANY" / Empty -> ('ANY', 'ANY')
    """
    if not binding_str or not str(binding_str).strip():
        return default_hw, default_inst
    
    clean = str(binding_str).strip()
    if clean.upper() in ('ANY', '*', 'ALL', 'UNLOCKED', 'NONE'):
        return 'ANY', 'ANY'
        
    for sep in ['/', '|', ',']:
        if sep in clean:
            parts = [p.strip() for p in clean.split(sep) if p.strip()]
            if len(parts) >= 2:
                p1, p2 = parts[0], parts[1]
                if p1.upper().startswith('INST-') and not p2.upper().startswith('INST-'):
                    return p2, p1
                return p1, p2
            elif len(parts) == 1:
                clean = parts[0]
                break

    tokens = clean.split()
    if len(tokens) == 2 and any(t.upper().startswith('INST-') for t in tokens):
        if tokens[0].upper().startswith('INST-'):
            return tokens[1], tokens[0]
        else:
            return tokens[0], tokens[1]

    if clean.upper().startswith('INST-'):
        return default_hw, clean
    return clean, default_inst


def check_hardware_binding_conflict(hardware_id, instance_id='ANY', exclude_license_id=None):
    """
    Checks if a concrete hardware_id and/or instance_id is already bound to another active or suspended license.
    Returns (conflict_found: bool, conflicting_license: dict | None, message: str)
    Wildcards ('ANY', '*', 'ALL', 'UNLOCKED', '', None) do not conflict.
    """
    clean_hw = normalize_hardware_id(hardware_id)
    if clean_hw in ('', 'ANY', 'ALL', 'UNLOCKED', '*'):
        clean_hw = None
        
    clean_inst = str(instance_id).strip().upper() if instance_id else None
    if clean_inst in ('', 'ANY', 'ALL', 'UNLOCKED', '*'):
        clean_inst = None
        
    # If both are wildcards, no hardware-locking conflict exists
    if not clean_hw and not clean_inst:
        return False, None, ""
        
    sql = "SELECT id, license_id, client_name, hardware_id, instance_id, status FROM master_licenses WHERE status IN ('active', 'suspended')"
    params = []
    if exclude_license_id:
        sql += " AND id != ?"
        params.append(int(exclude_license_id))
        
    existing_licenses = query_all(sql, tuple(params))
    
    for row in existing_licenses:
        row_hw = normalize_hardware_id(row.get('hardware_id'))
        if row_hw in ('', 'ANY', 'ALL', 'UNLOCKED', '*'):
            row_hw = None
            
        row_inst = str(row.get('instance_id') or '').strip().upper()
        if row_inst in ('', 'ANY', 'ALL', 'UNLOCKED', '*'):
            row_inst = None
            
        if not row_hw and not row_inst:
            continue
            
        # 1. Matching Hardware
        if clean_hw and row_hw and clean_hw == row_hw:
            # If both specify concrete instance_id and they match, or either has ANY (locks entire hardware)
            if (clean_inst and row_inst and clean_inst == row_inst) or (not clean_inst or not row_inst):
                msg = f"بصمة العتاد ({row['hardware_id']}) مرتبطة بالفعل بالترخيص النشط [{row['license_id']}] للعميل ({row['client_name']}). لا يمكن ربط أكثر من ترخيص نشط لنفس العتاد."
                return True, row, msg
                
        # 2. Matching Instance UUID
        if clean_inst and row_inst and clean_inst == row_inst:
            msg = f"معرف النسخة ({row['instance_id']}) مرتبط بالفعل بالترخيص النشط [{row['license_id']}] للعميل ({row['client_name']})."
            return True, row, msg
            
    return False, None, ""



# ----------------- Authentication Routes -----------------
def _license_session_limit(lic):
    """Read signed capacity without converting inventory limits or replacing zero."""
    try:
        return json.loads(lic['raw_json'])['payload'].get('limits', {}).get('max_active_sessions')
    except (ValueError, TypeError, KeyError):
        return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session.get('master_admin'):
            return redirect(url_for('dashboard'))
        return render_template('login.html')
        
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    
    from core.admin_passwords import verify_password, generate_password_hash
    user = query_one("SELECT * FROM master_users WHERE username = ?", (username,))
    valid, legacy = verify_password(user['password_hash'], password) if user else (False, False)

    if valid:
        if legacy:
            execute_write("UPDATE master_users SET password_hash=? WHERE id=? AND password_hash=?",
                          (generate_password_hash(password), user['id'], user['password_hash']))
        session.clear()
        session.permanent = True
        session['master_admin'] = user['username']
        session['admin_name'] = user['full_name'] or user['username']
        log_audit('LOGIN_SUCCESS', 'user', user['id'], f"تسجيل دخول ناجح من {request.remote_addr}", user['username'], request.remote_addr)
        flash('مرحباً بك في لوحة إدارة وحماية التراخيص المركزية', 'success')
        return redirect(url_for('dashboard'))
    else:
        log_audit('LOGIN_FAILED', 'user', None, f"محاولة دخول فاشلة للمستخدم: {username}", username, request.remote_addr)
        flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'danger')
        return redirect(url_for('login'))


@app.route('/logout')
def logout():
    admin = session.get('master_admin', 'admin')
    log_audit('LOGOUT', 'user', None, "تسجيل خروج", admin, request.remote_addr)
    session.clear()
    flash('تم تسجيل الخروج بنجاح', 'info')
    return redirect(url_for('login'))


# ----------------- Dashboard & Statistics -----------------
@app.route('/')
@login_required
def dashboard():
    total_licenses = query_one("SELECT COUNT(*) as c FROM master_licenses")['c']
    active_licenses = query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'active'")['c']
    suspended_licenses = query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'suspended'")['c']
    revoked_licenses = query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'revoked'")['c']
    expired_licenses = query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'expired'")['c']
    
    total_instances = query_one("SELECT COUNT(*) as c FROM master_instances")['c']
    unlicensed_instances = query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'unlicensed'")['c']
    
    since_10m = (datetime.datetime.utcnow() - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    live_online_instances = query_one("SELECT COUNT(*) as c FROM master_instances WHERE last_ping_at >= ?", (since_10m,))['c']
    
    total_clients = query_one("SELECT COUNT(*) as c FROM master_clients")['c']
    
    recent_licenses = query_all("SELECT * FROM master_licenses ORDER BY id DESC LIMIT 5")
    recent_instances = query_all("SELECT * FROM master_instances ORDER BY last_ping_at DESC LIMIT 6")
    recent_telemetry = query_all("SELECT * FROM master_telemetry ORDER BY id DESC LIMIT 6")
    
    stats = {
        "total_licenses": total_licenses,
        "active_licenses": active_licenses,
        "suspended_licenses": suspended_licenses,
        "revoked_licenses": revoked_licenses,
        "expired_licenses": expired_licenses,
        "total_instances": total_instances,
        "live_online_instances": live_online_instances,
        "unlicensed_instances": unlicensed_instances,
        "total_clients": total_clients
    }
    
    return render_template(
        'dashboard.html',
        stats=stats,
        recent_licenses=recent_licenses,
        recent_instances=recent_instances,
        recent_telemetry=recent_telemetry
    )


# ----------------- Live Discovered Instances Monitoring -----------------
@app.route('/instances')
@login_required
def instances_list():
    status_filter = request.args.get('status', 'all').strip()
    search = request.args.get('q', '').strip()
    
    sql = "SELECT * FROM master_instances WHERE 1=1"
    params = []
    
    since_10m = (datetime.datetime.utcnow() - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    
    if status_filter == 'online':
        sql += " AND last_ping_at >= ?"
        params.append(since_10m)
    elif status_filter == 'offline':
        sql += " AND (last_ping_at < ? OR last_ping_at IS NULL)"
        params.append(since_10m)
    elif status_filter != 'all' and status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
        
    if search:
        sql += " AND (instance_uuid LIKE ? OR server_hwid LIKE ? OR client_name LIKE ? OR client_ip LIKE ? OR license_id LIKE ?)"
        like_term = f"%{search}%"
        params.extend([like_term, like_term, like_term, like_term, like_term])
        
    sql += " ORDER BY last_ping_at DESC"
    instances = query_all(sql, tuple(params))
    
    # Enrich with online status flag
    for inst in instances:
        last_ping = inst.get('last_ping_at')
        if last_ping:
            try:
                lp_dt = parse_flexible_datetime(last_ping)
                diff_sec = (datetime.datetime.utcnow() - lp_dt).total_seconds()
                inst['is_online'] = diff_sec <= 600
                inst['diff_minutes'] = int(diff_sec // 60)
            except Exception:
                inst['is_online'] = False
                inst['diff_minutes'] = 9999
        else:
            inst['is_online'] = False
            inst['diff_minutes'] = 9999
            
    # Summary stats for filter pills
    counts = {
        'all': query_one("SELECT COUNT(*) as c FROM master_instances")['c'],
        'active': query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'active'")['c'],
        'unlicensed': query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'unlicensed'")['c'],
        'suspended': query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'suspended'")['c'],
        'revoked': query_one("SELECT COUNT(*) as c FROM master_instances WHERE status = 'revoked'")['c'],
        'online': query_one("SELECT COUNT(*) as c FROM master_instances WHERE last_ping_at >= ?", (since_10m,))['c']
    }
    
    # Available active licenses for one-click binding
    active_licenses = query_all("SELECT license_id, client_name, plan_tier, hardware_id FROM master_licenses WHERE status = 'active' ORDER BY id DESC")
    
    return render_template(
        'instances.html',
        instances=instances,
        counts=counts,
        current_status=status_filter,
        search_query=search,
        active_licenses=active_licenses
    )


@app.route('/instances/<int:instance_id>/action', methods=['POST'])
@login_required
def instance_remote_action(instance_id):
    action = request.form.get('action', '').strip()
    notes = request.form.get('notes', '').strip()
    license_id = request.form.get('license_id', '').strip()
    
    inst = query_one("SELECT * FROM master_instances WHERE id = ?", (instance_id,))
    if not inst:
        flash('النسخة المطلوبة غير موجودة', 'danger')
        return redirect(url_for('instances_list'))
        
    admin = session.get('master_admin', 'admin')
    
    if action == 'suspend':
        execute_write("UPDATE master_instances SET status = 'suspended', remote_action = 'suspend', notes = ? WHERE id = ?", (notes or 'تم التعليق من الإدارة المركزية', instance_id))
        log_audit('INSTANCE_SUSPEND', 'instance', instance_id, f"تعليق النسخة {inst['instance_uuid']}", admin, request.remote_addr)
        flash(f"تم إرسال أمر التعليق الفوري للنسخة {inst['instance_uuid']}", 'warning')
        
    elif action == 'activate':
        execute_write("UPDATE master_instances SET status = 'active', remote_action = 'activate' WHERE id = ?", (instance_id,))
        log_audit('INSTANCE_ACTIVATE', 'instance', instance_id, f"تفعيل النسخة {inst['instance_uuid']}", admin, request.remote_addr)
        flash(f"تم تفعيل النسخة {inst['instance_uuid']} بنجاح", 'success')
        
    elif action == 'revoke':
        execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke', notes = ? WHERE id = ?", (notes or 'تم إلغاء الترخيص وحظر النسخة', instance_id))
        log_audit('INSTANCE_REVOKE', 'instance', instance_id, f"إلغاء وحظر النسخة {inst['instance_uuid']}", admin, request.remote_addr)
        flash(f"تم حظر وإلغاء ترخيص النسخة {inst['instance_uuid']}", 'danger')
        
    elif action == 'assign_license' and license_id:
        lic = query_one("SELECT * FROM master_licenses WHERE license_id = ?", (license_id,))
        if lic:
            execute_write(
                "UPDATE master_instances SET license_id = ?, client_name = ?, status = 'active', remote_action = 'sync_license', remote_action_payload = ? WHERE id = ?",
                (license_id, lic['client_name'], lic['token_b64'], instance_id)
            )
            log_audit('INSTANCE_ASSIGN_LICENSE', 'instance', instance_id, f"ربط الترخيص {license_id} بالنسخة {inst['instance_uuid']}", admin, request.remote_addr)
            flash(f"تم ربط الترخيص {license_id} بالنسخة بنجاح وتجهيز التحديث عبر الهواء", 'success')
            
    elif action == 'delete':
        execute_write("DELETE FROM master_instances WHERE id = ?", (instance_id,))
        log_audit('INSTANCE_DELETE', 'instance', instance_id, f"حذف سجل النسخة {inst['instance_uuid']}", admin, request.remote_addr)
        flash('تم حذف سجل النسخة بنجاح', 'info')

    elif action == 'release_installation':
        from database.db import get_db
        conn = get_db()
        try:
            with conn:
                conn.execute("UPDATE master_instances SET status='revoked',remote_action='revoke' WHERE id=?", (instance_id,))
                conn.execute('DELETE FROM master_license_installations WHERE instance_uuid=?', (inst['instance_uuid'],))
        finally:
            conn.close()
        log_audit('INSTALLATION_RELEASED', 'instance', instance_id, 'Revoked instance and released its installation capacity')
        flash('أُلغي اعتماد النسخة وحُرر مقعد تثبيتها.', 'success')
        
    return redirect(url_for('instances_list'))


# ----------------- Licenses Management -----------------
@app.route('/licenses')
@login_required
def licenses_list():
    status_filter = request.args.get('status', 'all').strip()
    search = request.args.get('q', '').strip()
    
    sql = "SELECT l.*, c.name as client_full_name, c.network_name FROM master_licenses l LEFT JOIN master_clients c ON l.client_id = c.id WHERE 1=1"
    params = []
    
    if status_filter != 'all' and status_filter:
        sql += " AND l.status = ?"
        params.append(status_filter)
        
    if search:
        sql += " AND (l.license_id LIKE ? OR l.client_name LIKE ? OR l.hardware_id LIKE ? OR l.instance_id LIKE ? OR c.network_name LIKE ?)"
        like_term = f"%{search}%"
        params.extend([like_term, like_term, like_term, like_term, like_term])
        
    sql += " ORDER BY l.id DESC"
    licenses = query_all(sql, tuple(params))
    
    # Calculate days left for each
    now = datetime.datetime.utcnow()
    for lic in licenses:
        exp_str = lic.get('expires_at')
        if exp_str:
            try:
                exp_dt = parse_flexible_datetime(exp_str)
                diff = (exp_dt - now).days
                lic['days_left'] = max(0, diff)
                lic['is_expired'] = exp_dt < now
            except Exception:
                lic['days_left'] = 0
                lic['is_expired'] = False
        else:
            lic['days_left'] = 9999
            lic['is_expired'] = False
            
    counts = {
        'all': query_one("SELECT COUNT(*) as c FROM master_licenses")['c'],
        'active': query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'active'")['c'],
        'suspended': query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'suspended'")['c'],
        'revoked': query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'revoked'")['c'],
        'expired': query_one("SELECT COUNT(*) as c FROM master_licenses WHERE status = 'expired'")['c']
    }
    
    clients = query_all("SELECT id, name, network_name FROM master_clients ORDER BY name ASC")
    
    return render_template(
        'licenses_list.html',
        licenses=licenses,
        counts=counts,
        current_status=status_filter,
        search_query=search,
        clients=clients
    )


@app.route('/licenses/generate', methods=['GET', 'POST'])
@login_required
def license_generator():
    if request.method == 'GET':
        client_id_arg = request.args.get('client_id')
        client_name_arg = request.args.get('client_name')
        selected_client = None
        if client_id_arg and str(client_id_arg).isdigit():
            selected_client = query_one("SELECT * FROM master_clients WHERE id = ?", (int(client_id_arg),))
        elif client_name_arg:
            selected_client = query_one("SELECT * FROM master_clients WHERE name = ?", (client_name_arg.strip(),))
            
        clients = query_all("SELECT id, name, network_name FROM master_clients ORDER BY name ASC")
        return render_template('license_generator.html', clients=clients, selected_client=selected_client)
        
    # Form data
    client_name = request.form.get('client_name', '').strip()
    client_id = request.form.get('client_id')
    client_id = int(client_id) if client_id and client_id.isdigit() else None
    
    server_binding = request.form.get('server_binding', '').strip()
    if server_binding:
        hardware_id, instance_id = parse_server_binding_input(server_binding)
    else:
        hardware_id = request.form.get('hardware_id', 'ANY').strip()
        instance_id = request.form.get('instance_id', 'ANY').strip()
        if '/' in hardware_id or '|' in hardware_id or hardware_id.upper().startswith('INST-'):
            hardware_id, instance_id = parse_server_binding_input(hardware_id, hardware_id, instance_id)
            
    license_type = request.form.get('license_type', 'single').strip()
    max_instances = int(request.form.get('max_instances', 1))
    
    plan_tier = request.form.get('plan_tier', 'Enterprise').strip()
    max_active_sessions = int(request.form.get('max_active_sessions', 500))
    max_nas = int(request.form.get('max_nas', 15))
    max_managers = int(request.form.get('max_managers', 5))
    
    validity_type = request.form.get('validity_type', 'days')
    days_valid = int(request.form.get('days_valid', 365))
    is_lifetime = (validity_type == 'lifetime')
    
    # Custom License ID or auto-generated
    custom_lic_id = request.form.get('license_id', '').strip()
    if not custom_lic_id:
        import uuid
        rnd_hex = uuid.uuid4().hex[:8].upper()
        custom_lic_id = f"MAX-{datetime.datetime.utcnow().year}-{rnd_hex}"
        
    # Validation: Prevent duplicate hardware/instance binding on active licenses
    conflict, c_lic, c_msg = check_hardware_binding_conflict(hardware_id, instance_id)
    if conflict:
        flash(f"⚠️ تعذر إصدار الترخيص: {c_msg}", 'danger')
        clients = query_all("SELECT id, name, network_name FROM master_clients ORDER BY name ASC")
        return render_template('license_generator.html', clients=clients)
        
    features = {
        "user_portal": request.form.get('feat_user_portal') == '1',
        "automation_rules": request.form.get('feat_automation') == '1',
        "gis_map": request.form.get('feat_gis') == '1',
        "autoheal": request.form.get('feat_autoheal') == '1',
        "accounting_archiver": request.form.get('feat_archiver') == '1',
        "radius_simulator": request.form.get('feat_simulator') == '1',
        "traffic_analytics": request.form.get('feat_analytics') == '1',
        "api_access": request.form.get('feat_api') == '1',
        "white_label": request.form.get('feat_whitelabel') == '1',
        "whatsapp_gateway": request.form.get('feat_whatsapp') == '1',
        "vpn_tunnels": request.form.get('feat_vpn_tunnels') == '1',
        "loyalty_rewards": request.form.get('feat_loyalty', '1') == '1' if 'feat_loyalty' in request.form else True,
        "card_designer": request.form.get('feat_card_designer', '1') == '1' if 'feat_card_designer' in request.form else True
    }
    
    pkg = generate_license_package(
        license_id=custom_lic_id,
        client_name=client_name,
        hardware_id=hardware_id,
        instance_id=instance_id,
        license_type=license_type,
        max_instances=max_instances,
        plan_tier=plan_tier,
        max_active_sessions=max_active_sessions,
        max_nas=max_nas,
        max_managers=max_managers,
        features=features,
        days_valid=days_valid,
        is_lifetime=is_lifetime,
        client_id=client_id,
        max_subscribers=0
    )
    
    # Save into master_licenses DB
    admin = session.get('master_admin', 'admin')
    execute_write("""
    INSERT INTO master_licenses (
        license_id, client_id, client_name, hardware_id, instance_id, license_type, max_instances,
        plan_tier, max_active_sessions, max_subscribers, max_nas, max_managers, features_json,
        token_b64, raw_json, signature_b64, issued_at, expires_at, status, notes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
    """, (
        pkg['license_id'], client_id, client_name, pkg['hardware_id'], pkg['instance_id'], pkg['license_type'], pkg['max_instances'],
        plan_tier, max_active_sessions, 0, max_nas, max_managers, json.dumps(features),
        pkg['token_b64'], pkg['raw_json'], pkg['signature_b64'], pkg['issued_at'], pkg['expires_at'], request.form.get('notes', '')
    ))
    
    log_audit('LICENSE_CREATE', 'license', pkg['license_id'], f"إصدار ترخيص جديد للعميل: {client_name} ({pkg['license_id']})", admin, request.remote_addr)
    flash(f"تم إصدار وتوقيع الترخيص {pkg['license_id']} بنجاح 🟢", 'success')
    
    return render_template('license_success.html', pkg=dict(pkg, **activation_bundle(pkg['license_id'])))


@app.route('/licenses/quick-trial', methods=['POST'])
@login_required
def license_quick_trial():
    """Generates an instant 7, 14, or 30-day Trial License in 1-Click."""
    client_name = request.form.get('client_name', '').strip()
    if not client_name:
        client_name = f"عميل تجريبي {datetime.datetime.utcnow().strftime('%m%d-%H%M')}"
        
    days = int(request.form.get('days', get_setting('trial_default_days', '14')))
    sessions = int(request.form.get('max_active_sessions', get_setting('trial_default_sessions', '100')))
    nas = int(request.form.get('max_nas', get_setting('trial_default_nas', '3')))
    
    rnd_hex = hashlib.sha256(f"TRIAL_{datetime.datetime.utcnow().timestamp()}_{client_name}".encode()).hexdigest()[:8].upper()
    lic_id = f"MAX-TRIAL-{datetime.datetime.utcnow().year}-{rnd_hex}"
    
    features = {
        "user_portal": True,
        "automation_rules": True,
        "gis_map": False,
        "autoheal": True,
        "accounting_archiver": False,
        "radius_simulator": True,
        "traffic_analytics": True,
        "api_access": False,
        "white_label": False,
        "whatsapp_gateway": False
    }
    
    pkg = generate_license_package(
        license_id=lic_id,
        client_name=client_name,
        hardware_id="ANY",
        instance_id="ANY",
        license_type="trial",
        max_instances=1,
        plan_tier="Trial",
        max_active_sessions=sessions,
        max_nas=nas,
        max_managers=2,
        features=features,
        days_valid=days,
        max_subscribers=0
    )
    
    admin = session.get('master_admin', 'admin')
    execute_write("""
    INSERT INTO master_licenses (
        license_id, client_name, hardware_id, instance_id, license_type, max_instances,
        plan_tier, max_active_sessions, max_subscribers, max_nas, max_managers, features_json,
        token_b64, raw_json, signature_b64, issued_at, expires_at, status, notes
    ) VALUES (?, ?, 'ANY', 'ANY', 'trial', 1, 'Trial', ?, 0, ?, 2, ?, ?, ?, ?, ?, ?, 'active', ?)
    """, (
        pkg['license_id'], client_name, sessions, nas, json.dumps(features),
        pkg['token_b64'], pkg['raw_json'], pkg['signature_b64'], pkg['issued_at'], pkg['expires_at'], f"ترخيص تجريبي لمدة {days} يوم"
    ))
    
    log_audit('LICENSE_TRIAL_CREATE', 'license', pkg['license_id'], f"إصدار ترخيص تجريبي ({days} يوم) للعميل: {client_name}", admin, request.remote_addr)
    notify_trial_issued(pkg)
    flash(f"تم إصدار وتوقيع الترخيص التجريبي {pkg['license_id']} بنجاح ⚡", 'success')
    
    return render_template('license_success.html', pkg=dict(pkg, **activation_bundle(pkg['license_id'])))


@app.route('/licenses/<int:lic_id>/quick-action', methods=['POST'])
@login_required
def license_quick_action(lic_id):
    action = request.form.get('action', '').strip()
    admin = session.get('master_admin', 'admin')
    
    lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
    if not lic:
        flash('الترخيص غير موجود', 'danger')
        return redirect(url_for('licenses_list'))
        
    if action == 'suspend':
        execute_write("UPDATE master_licenses SET status = 'suspended' WHERE id = ?", (lic_id,))
        # Also queue remote action on matching instances
        execute_write("UPDATE master_instances SET status = 'suspended', remote_action = 'suspend' WHERE license_id = ?", (lic['license_id'],))
        log_audit('LICENSE_SUSPEND', 'license', lic['license_id'], f"تعليق الترخيص {lic['license_id']}", admin, request.remote_addr)
        flash(f"تم تعليق الترخيص {lic['license_id']} فورياً", 'warning')
        
    elif action == 'activate':
        execute_write("UPDATE master_licenses SET status = 'active' WHERE id = ?", (lic_id,))
        execute_write("UPDATE master_instances SET status = 'active', remote_action = 'activate' WHERE license_id = ?", (lic['license_id'],))
        log_audit('LICENSE_ACTIVATE', 'license', lic['license_id'], f"إعادة تفعيل الترخيص {lic['license_id']}", admin, request.remote_addr)
        flash(f"تم تفعيل الترخيص {lic['license_id']} بنجاح", 'success')
        
    elif action == 'revoke':
        reason = request.form.get('reason', 'تم الإلغاء والحظر من الإدارة')
        execute_write("UPDATE master_licenses SET status = 'revoked', revocation_reason = ? WHERE id = ?", (reason, lic_id))
        execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke' WHERE license_id = ?", (lic['license_id'],))
        log_audit('LICENSE_REVOKE', 'license', lic['license_id'], f"إلغاء الترخيص {lic['license_id']}: {reason}", admin, request.remote_addr)
        flash(f"تم حظر وإلغاء الترخيص {lic['license_id']}", 'danger')
        
    elif action == 'extend':
        extra_days = int(request.form.get('extra_days', 30))
        cur_exp = parse_flexible_datetime(lic['expires_at'])
        now = datetime.datetime.utcnow()
        base_date = max(cur_exp, now)
        new_exp_dt = base_date + datetime.timedelta(days=extra_days)
        new_exp_str = new_exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Re-sign payload
        features = json.loads(lic['features_json']) if lic.get('features_json') else {}
        active_sess = _license_session_limit(lic)
        re_pkg = generate_license_package(
            license_id=lic['license_id'],
            client_name=lic['client_name'],
            hardware_id=lic['hardware_id'],
            instance_id=lic.get('instance_id', 'ANY'),
            license_type=lic.get('license_type', 'single'),
            max_instances=lic.get('max_instances', 1),
            plan_tier=lic['plan_tier'],
            max_active_sessions=active_sess,
            max_nas=lic['max_nas'],
            max_managers=lic['max_managers'],
            features=features,
            days_valid=extra_days,
            custom_expires_at=new_exp_str,
            client_id=lic['client_id'],
            max_subscribers=(int(lic.get('max_subscribers') or 0) if active_sess is None else 0)
        )
        
        execute_write("""
        UPDATE master_licenses SET
            expires_at = ?, max_active_sessions = ?, token_b64 = ?, raw_json = ?, signature_b64 = ?, status = 'active'
        WHERE id = ?
        """, (new_exp_str, active_sess, re_pkg['token_b64'], re_pkg['raw_json'], re_pkg['signature_b64'], lic_id))
        
        # Queue OTA sync for connected instances
        execute_write(
            "UPDATE master_instances SET remote_action = 'sync_license', remote_action_payload = ?, status = 'active' WHERE license_id = ?",
            (re_pkg['token_b64'], lic['license_id'])
        )
        
        log_audit('LICENSE_EXTEND', 'license', lic['license_id'], f"تمديد الترخيص {lic['license_id']} لمدة {extra_days} يوم إضافية", admin, request.remote_addr)
        flash(f"تم تمديد صلاحية الترخيص {lic['license_id']} حتى {new_exp_str[:10]} بنجاح وتحديث التوقيع الرقمي عبر الهواء", 'success')
        
    elif action in ('edit_license_full', 'upgrade_quota'):
        client_name = request.form.get('client_name', lic['client_name']).strip()
        plan_tier = request.form.get('plan_tier', lic['plan_tier']).strip()
        license_type = request.form.get('license_type', lic.get('license_type', 'single')).strip()
        server_binding = request.form.get('server_binding', '').strip()
        if server_binding:
            hardware_id, instance_id = parse_server_binding_input(server_binding, lic['hardware_id'], lic.get('instance_id', 'ANY'))
        else:
            hardware_id = request.form.get('hardware_id', lic['hardware_id']).strip()
            instance_id = request.form.get('instance_id', lic.get('instance_id', 'ANY')).strip()
            if '/' in hardware_id or '|' in hardware_id or hardware_id.upper().startswith('INST-'):
                hardware_id, instance_id = parse_server_binding_input(hardware_id, hardware_id, instance_id)
        
        # Validation: Prevent duplicate hardware/instance binding on active licenses
        conflict, c_lic, c_msg = check_hardware_binding_conflict(hardware_id, instance_id, exclude_license_id=lic_id)
        if conflict:
            flash(f"⚠️ تعذر حفظ التعديل: {c_msg}", 'danger')
            return redirect(url_for('licenses_list'))

        new_sessions = int(request.form['max_active_sessions']) if request.form.get('max_active_sessions', '').strip() else _license_session_limit(lic)
        new_nas = int(request.form.get('max_nas', lic['max_nas']))
        new_managers = int(request.form.get('max_managers', lic.get('max_managers', 5)))
        new_instances = int(request.form.get('max_instances', lic.get('max_instances', 1)))
        notes = request.form.get('notes', lic.get('notes', ''))
        
        if action == 'edit_license_full' or request.form.get('feat_form_submitted') == '1':
            features = {
                "user_portal": request.form.get('feat_user_portal') == '1',
                "automation_rules": request.form.get('feat_automation') == '1',
                "gis_map": request.form.get('feat_gis') == '1',
                "autoheal": request.form.get('feat_autoheal') == '1',
                "accounting_archiver": request.form.get('feat_archiver') == '1',
                "radius_simulator": request.form.get('feat_simulator') == '1',
                "traffic_analytics": request.form.get('feat_analytics') == '1',
                "api_access": request.form.get('feat_api') == '1',
                "white_label": request.form.get('feat_whitelabel') == '1',
                "whatsapp_gateway": request.form.get('feat_whatsapp') == '1',
                "vpn_tunnels": request.form.get('feat_vpn_tunnels') == '1',
                "loyalty_rewards": request.form.get('feat_loyalty') == '1',
                "card_designer": request.form.get('feat_card_designer') == '1'
            }
        else:
            features = json.loads(lic['features_json']) if lic.get('features_json') else {}
            
        validity_mode = request.form.get('validity_mode', 'keep')
        is_lifetime = False
        custom_expires_at = None
        
        if validity_mode == 'lifetime':
            is_lifetime = True
        elif validity_mode == 'extend':
            extra_days = int(request.form.get('extend_days', 30))
            cur_exp = parse_flexible_datetime(lic['expires_at'])
            now = datetime.datetime.utcnow()
            base_date = max(cur_exp, now)
            new_exp_dt = base_date + datetime.timedelta(days=extra_days)
            custom_expires_at = new_exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif validity_mode == 'custom_date':
            c_date = request.form.get('custom_date', '').strip()
            if c_date:
                if len(c_date) == 10:
                    c_date += "T23:59:59Z"
                custom_expires_at = c_date
            else:
                custom_expires_at = lic['expires_at']
        else:
            custom_expires_at = lic['expires_at']
            if lic.get('expires_at') and int(lic['expires_at'][:4]) > 2090:
                is_lifetime = True
                
        re_pkg = generate_license_package(
            license_id=lic['license_id'],
            client_name=client_name,
            hardware_id=hardware_id,
            instance_id=instance_id,
            license_type=license_type,
            max_instances=new_instances,
            plan_tier=plan_tier,
            max_active_sessions=new_sessions,
            max_nas=new_nas,
            max_managers=new_managers,
            features=features,
            days_valid=365,
            is_lifetime=is_lifetime,
            client_id=lic['client_id'],
            custom_expires_at=custom_expires_at,
            max_subscribers=(int(lic.get('max_subscribers') or 0) if new_sessions is None else 0)
        )
        
        execute_write("""
        UPDATE master_licenses SET
            client_name = ?,
            plan_tier = ?,
            license_type = ?,
            hardware_id = ?,
            instance_id = ?,
            max_active_sessions = ?,
                max_nas = ?,
            max_managers = ?,
            max_instances = ?,
            features_json = ?,
            expires_at = ?,
            token_b64 = ?,
            raw_json = ?,
            signature_b64 = ?,
            notes = ?
        WHERE id = ?
        """, (
            client_name, plan_tier, license_type, hardware_id, instance_id,
            new_sessions, new_nas, new_managers, new_instances,
            json.dumps(features), re_pkg['expires_at'],
            re_pkg['token_b64'], re_pkg['raw_json'], re_pkg['signature_b64'],
            notes, lic_id
        ))
        
        execute_write(
            "UPDATE master_instances SET remote_action = 'sync_license', remote_action_payload = ? WHERE license_id = ?",
            (re_pkg['token_b64'], lic['license_id'])
        )
        
        log_audit('LICENSE_EDIT', 'license', lic['license_id'], f"تعديل شامل للترخيص {lic['license_id']} (العميل: {client_name}, الجلسات: {new_sessions}, الراوترات: {new_nas})", admin, request.remote_addr)
        flash(f"تم حفظ تعديلات الترخيص {lic['license_id']} بنجاح وإعادة توقيعه رقمياً وبث التحديث OTA 🟢", 'success')
        
    elif action == 'reset_hwid':
        binding_raw = request.form.get('new_server_binding', '').strip() or request.form.get('new_hardware_id', 'ANY').strip()
        new_hw, new_inst = parse_server_binding_input(binding_raw)
        
        # Validation: check duplicate binding against other active licenses
        conflict, c_lic, c_msg = check_hardware_binding_conflict(new_hw, new_inst, exclude_license_id=lic_id)
        if conflict:
            flash(f"⚠️ تعذر نقل الترخيص: {c_msg}", 'danger')
            return redirect(url_for('licenses_list'))
            
        features = json.loads(lic['features_json']) if lic.get('features_json') else {}
        is_lifetime = (lic.get('expires_at') and int(lic['expires_at'][:4]) > 2090)
        active_sess = _license_session_limit(lic)
        
        re_pkg = generate_license_package(
            license_id=lic['license_id'],
            client_name=lic['client_name'],
            hardware_id=new_hw,
            instance_id=new_inst,
            license_type=lic.get('license_type', 'single'),
            max_instances=lic.get('max_instances', 1),
            plan_tier=lic['plan_tier'],
            max_active_sessions=active_sess,
            max_nas=lic['max_nas'],
            max_managers=lic.get('max_managers', 5),
            features=features,
            days_valid=365,
            is_lifetime=is_lifetime,
            client_id=lic['client_id'],
            custom_expires_at=lic.get('expires_at'),
            max_subscribers=(int(lic.get('max_subscribers') or 0) if active_sess is None else 0)
        )
        
        execute_write("""
        UPDATE master_licenses SET
            hardware_id = ?, instance_id = ?, max_active_sessions = ?, token_b64 = ?, raw_json = ?, signature_b64 = ?, status = 'active'
        WHERE id = ?
        """, (re_pkg['hardware_id'], re_pkg['instance_id'], active_sess, re_pkg['token_b64'], re_pkg['raw_json'], re_pkg['signature_b64'], lic_id))
        
        # Broadcast OTA update / queue sync for connected instance
        execute_write(
            "UPDATE master_instances SET remote_action = 'sync_license', remote_action_payload = ?, status = 'active' WHERE license_id = ?",
            (re_pkg['token_b64'], lic['license_id'])
        )
        
        exp_display = lic['expires_at'][:10] if lic.get('expires_at') else "دائم"
        log_audit('LICENSE_TRANSFER_HWID', 'license', lic['license_id'], 
                  f"نقل الترخيص {lic['license_id']} للعميل {lic['client_name']} إلى السيرفر الجديد ({new_hw} / {new_inst}) مع الحفاظ التام على كامل خصائص الاشتراك (الانتهاء: {exp_display}, سعة الجلسات: {active_sess}, الراوترات: {lic['max_nas']})", 
                  admin, request.remote_addr)
        flash(f"تم نقل الترخيص {lic['license_id']} بنجاح إلى السيرفر الجديد ({new_hw}) مع الحفاظ التام على الأيام المتبقية (حتى {exp_display}) والميزات وسعة الجلسات المتصلة 🟢", 'success')

        
    elif action == 'delete':
        execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke' WHERE license_id = ?", (lic['license_id'],))
        execute_write("DELETE FROM master_licenses WHERE id = ?", (lic_id,))
        log_audit('LICENSE_DELETE', 'license', lic['license_id'], f"حذف الترخيص {lic['license_id']}", admin, request.remote_addr)
        flash('تم حذف سجل الترخيص بنجاح وإرسال أمر إقفال وحظر فوري للنسخ المتصلة', 'info')
        
    return redirect(url_for('licenses_list'))


@app.route('/licenses/<int:lic_id>/download')
@login_required
def download_license_file(lic_id):
    lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
    if not lic:
        flash('الترخيص غير موجود', 'danger')
        return redirect(url_for('licenses_list'))
        
    try:
        bundle = activation_bundle(lic['license_id'])
    except EnrollmentError:
        flash('لا يمكن إصدار ملف تفعيل لترخيص موقوف أو منتهي.', 'danger')
        return redirect(url_for('licenses_list'))
    mem_file = io.BytesIO(bundle['raw_json'].encode('utf-8'))
    filename = f"max_radius_{lic['license_id'].replace('-', '_').lower()}.lic"
    
    response = send_file(
        mem_file,
        mimetype='application/json',
        as_attachment=True,
        download_name=filename
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/licenses/<int:lic_id>/activation-code', methods=['POST'])
@login_required
def license_activation_code(lic_id):
    lic = query_one('SELECT license_id FROM master_licenses WHERE id=?', (lic_id,))
    if not lic:
        return jsonify(success=False, message='الترخيص غير موجود'), 404
    try:
        bundle = activation_bundle(lic['license_id'])
    except EnrollmentError:
        return jsonify(success=False, message='لا يمكن إصدار كود تفعيل لترخيص موقوف أو منتهي'), 400
    response = jsonify(success=True, token_b64=bundle['token_b64'])
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/licenses/bulk-action', methods=['POST'])
@login_required
def licenses_bulk_action():
    action = request.form.get('bulk_action', '').strip()
    selected_ids_raw = request.form.getlist('selected_ids')
    if not selected_ids_raw:
        ids_str = request.form.get('selected_ids_str', '').strip()
        if ids_str:
            selected_ids_raw = ids_str.split(',')
            
    selected_ids = [int(i.strip()) for i in selected_ids_raw if str(i).strip().isdigit()]
    
    if not selected_ids:
        flash("لم يتم تحديد أي ترخيص لتنفيذ العملية الجماعية ⚠️", "warning")
        return redirect(url_for('licenses_list'))
        
    admin = session.get('master_admin', 'admin')
    updated_count = 0
    now = datetime.datetime.utcnow()
    
    if action == 'extend':
        extra_days = int(request.form.get('bulk_extend_days', 30))
        for lic_id in selected_ids:
            lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
            if not lic:
                continue
            cur_exp = parse_flexible_datetime(lic['expires_at'])
            base_date = max(cur_exp, now)
            new_exp_dt = base_date + datetime.timedelta(days=extra_days)
            new_exp_str = new_exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            features = json.loads(lic['features_json']) if lic.get('features_json') else {}
            active_sess = _license_session_limit(lic)
            re_pkg = generate_license_package(
                license_id=lic['license_id'],
                client_name=lic['client_name'],
                hardware_id=lic['hardware_id'],
                instance_id=lic.get('instance_id', 'ANY'),
                license_type=lic.get('license_type', 'single'),
                max_instances=lic.get('max_instances', 1),
                plan_tier=lic['plan_tier'],
                max_active_sessions=active_sess,
                max_nas=lic['max_nas'],
                max_managers=lic.get('max_managers', 5),
                features=features,
                days_valid=extra_days,
                client_id=lic['client_id'],
                custom_expires_at=new_exp_str,
                max_subscribers=(int(lic.get('max_subscribers') or 0) if active_sess is None else 0)
            )
            execute_write("""
            UPDATE master_licenses SET
                expires_at = ?, max_active_sessions = ?, token_b64 = ?, raw_json = ?, signature_b64 = ?, status = 'active'
            WHERE id = ?
            """, (new_exp_str, active_sess, re_pkg['token_b64'], re_pkg['raw_json'], re_pkg['signature_b64'], lic_id))
            
            execute_write(
                "UPDATE master_instances SET remote_action = 'sync_license', remote_action_payload = ?, status = 'active' WHERE license_id = ?",
                (re_pkg['token_b64'], lic['license_id'])
            )
            updated_count += 1
            
        log_audit('LICENSE_BULK_EXTEND', 'license', None, f"تمديد جماعي لعدد {updated_count} ترخيص بمقدار {extra_days} يوم", admin, request.remote_addr)
        flash(f"تم تمديد صلاحية {updated_count} ترخيص بنجاح بمقدار {extra_days} يوم إضافية 🟢", "success")

    elif action == 'suspend':
        for lic_id in selected_ids:
            lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
            if lic:
                execute_write("UPDATE master_licenses SET status = 'suspended' WHERE id = ?", (lic_id,))
                execute_write("UPDATE master_instances SET status = 'suspended', remote_action = 'suspend' WHERE license_id = ?", (lic['license_id'],))
                updated_count += 1
        log_audit('LICENSE_BULK_SUSPEND', 'license', None, f"تعليق جماعي لعدد {updated_count} ترخيص", admin, request.remote_addr)
        flash(f"تم تعليق {updated_count} ترخيص مؤقتاً وبث أمر الإيقاف عبر الهواء ⚠️", "warning")

    elif action == 'activate':
        for lic_id in selected_ids:
            lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
            if lic:
                execute_write("UPDATE master_licenses SET status = 'active' WHERE id = ?", (lic_id,))
                execute_write("UPDATE master_instances SET status = 'active', remote_action = 'activate' WHERE license_id = ?", (lic['license_id'],))
                updated_count += 1
        log_audit('LICENSE_BULK_ACTIVATE', 'license', None, f"تفعيل جماعي لعدد {updated_count} ترخيص", admin, request.remote_addr)
        flash(f"تم تفعيل {updated_count} ترخيص بنجاح وبث أمر التشغيل 🟢", "success")

    elif action == 'upgrade_quota':
        sessions_add = int(request.form.get('bulk_sessions_add', 0))
        if sessions_add < 0:
            flash('ترقية السعة الجماعية تتطلب قيمة غير سالبة.', 'warning')
            return redirect(url_for('licenses_list'))
        nas_add = int(request.form.get('bulk_nas_add', 0))
        for lic_id in selected_ids:
            lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
            if not lic:
                continue
            cur_sess = _license_session_limit(lic)
            if cur_sess is None or int(cur_sess) == 0:
                continue
            cur_sess = int(cur_sess)
            new_sessions = cur_sess + sessions_add
            new_nas = max(1, int(lic['max_nas']) + nas_add)
            features = json.loads(lic['features_json']) if lic.get('features_json') else {}
            re_pkg = generate_license_package(
                license_id=lic['license_id'],
                client_name=lic['client_name'],
                hardware_id=lic['hardware_id'],
                instance_id=lic.get('instance_id', 'ANY'),
                license_type=lic.get('license_type', 'single'),
                max_instances=lic.get('max_instances', 1),
                plan_tier=lic['plan_tier'],
                max_active_sessions=new_sessions,
                max_nas=new_nas,
                max_managers=lic.get('max_managers', 5),
                features=features,
                custom_expires_at=lic['expires_at'],
                client_id=lic['client_id'],
                max_subscribers=(int(lic.get('max_subscribers') or 0) if new_sessions is None else 0)
            )
            execute_write("""
            UPDATE master_licenses SET
                max_active_sessions = ?, max_subscribers = 0, max_nas = ?, token_b64 = ?, raw_json = ?, signature_b64 = ?
            WHERE id = ?
            """, (new_sessions, new_nas, re_pkg['token_b64'], re_pkg['raw_json'], re_pkg['signature_b64'], lic_id))
            
            execute_write(
                "UPDATE master_instances SET remote_action = 'sync_license', remote_action_payload = ? WHERE license_id = ?",
                (re_pkg['token_b64'], lic['license_id'])
            )
            updated_count += 1
        log_audit('LICENSE_BULK_QUOTA', 'license', None, f"تعديل سعة جماعي لعدد {updated_count} ترخيص (+{sessions_add} جلسات, +{nas_add} راوترات)", admin, request.remote_addr)
        flash(f"تمت ترقية وتعديل سعات {updated_count} ترخيص وإعادة توقيعها وبثها OTA بنجاح 🟢", "success")

    elif action == 'revoke':
        for lic_id in selected_ids:
            lic = query_one("SELECT * FROM master_licenses WHERE id = ?", (lic_id,))
            if lic:
                execute_write("UPDATE master_licenses SET status = 'revoked', revocation_reason = 'حظر جماعي من الإدارة' WHERE id = ?", (lic_id,))
                execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke' WHERE license_id = ?", (lic['license_id'],))
                updated_count += 1
        log_audit('LICENSE_BULK_REVOKE', 'license', None, f"حظر وإلغاء جماعي لعدد {updated_count} ترخيص", admin, request.remote_addr)
        flash(f"تم إلغاء وحظر {updated_count} ترخيص نهائياً 🛑", "danger")

    return redirect(url_for('licenses_list'))


# ----------------- Clients Management -----------------
@app.route('/clients', methods=['GET', 'POST'])
@login_required
def clients_list():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        network_name = request.form.get('network_name', '').strip()
        phone = request.form.get('phone', '').strip()
        whatsapp = request.form.get('whatsapp', '').strip()
        email = request.form.get('email', '').strip()
        country = request.form.get('country', 'اليمن').strip()
        city = request.form.get('city', '').strip()
        notes = request.form.get('notes', '').strip()
        
        if name:
            new_id = execute_write("""
            INSERT INTO master_clients (name, network_name, phone, whatsapp, email, country, city, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, network_name, phone, whatsapp, email, country, city, notes))
            
            admin = session.get('master_admin', 'admin')
            log_audit('CLIENT_CREATE', 'client', new_id, f"إضافة عميل جديد: {name}", admin, request.remote_addr)
            flash(f"تمت إضافة العميل {name} بنجاح", 'success')
            return redirect(url_for('clients_list'))
            
    search = request.args.get('q', '').strip()
    sql = """
    SELECT c.*, 
           (SELECT COUNT(*) FROM master_licenses WHERE client_id = c.id) as licenses_count,
           (SELECT COUNT(*) FROM master_instances WHERE client_name = c.name) as instances_count
    FROM master_clients c
    WHERE 1=1
    """
    params = []
    if search:
        sql += " AND (c.name LIKE ? OR c.network_name LIKE ? OR c.phone LIKE ? OR c.city LIKE ?)"
        like_term = f"%{search}%"
        params.extend([like_term, like_term, like_term, like_term])
        
    sql += " ORDER BY c.id DESC"
    clients = query_all(sql, tuple(params))
    
    return render_template('clients.html', clients=clients, search_query=search)


@app.route('/clients/<int:client_id>/edit', methods=['POST'])
@login_required
def client_edit(client_id):
    name = request.form.get('name', '').strip()
    network_name = request.form.get('network_name', '').strip()
    phone = request.form.get('phone', '').strip()
    whatsapp = request.form.get('whatsapp', '').strip()
    email = request.form.get('email', '').strip()
    country = request.form.get('country', 'اليمن').strip()
    city = request.form.get('city', '').strip()
    notes = request.form.get('notes', '').strip()
    
    execute_write("""
    UPDATE master_clients SET
        name = ?, network_name = ?, phone = ?, whatsapp = ?, email = ?,
        country = ?, city = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (name, network_name, phone, whatsapp, email, country, city, notes, client_id))
    
    admin = session.get('master_admin', 'admin')
    log_audit('CLIENT_UPDATE', 'client', client_id, f"تعديل بيانات العميل: {name}", admin, request.remote_addr)
    flash(f"تم تحديث بيانات العميل {name} بنجاح", 'success')
    return redirect(url_for('clients_list'))


@app.route('/clients/<int:client_id>/delete', methods=['POST'])
@login_required
def client_delete(client_id):
    c = query_one("SELECT name FROM master_clients WHERE id = ?", (client_id,))
    if c:
        execute_write("DELETE FROM master_clients WHERE id = ?", (client_id,))
        admin = session.get('master_admin', 'admin')
        log_audit('CLIENT_DELETE', 'client', client_id, f"حذف العميل: {c['name']}", admin, request.remote_addr)
        flash(f"تم حذف العميل {c['name']} بنجاح", 'info')
    return redirect(url_for('clients_list'))


# ----------------- Telemetry & Audit Logs -----------------
@app.route('/telemetry')
@login_required
def telemetry():
    logs = query_all("""
        SELECT t.*, l.client_name, l.plan_tier 
        FROM master_telemetry t 
        LEFT JOIN master_licenses l ON t.license_id = l.license_id 
        ORDER BY t.id DESC LIMIT 100
    """)
    return render_template('telemetry.html', telemetry=logs)


@app.route('/telemetry/clear', methods=['POST'])
@login_required
def telemetry_clear():
    execute_write("DELETE FROM master_telemetry")
    admin = session.get('master_admin', 'admin')
    log_audit('TELEMETRY_CLEAR', 'system', None, "مسح سجلات النبضات القديمة", admin, request.remote_addr)
    flash('تم مسح سجلات النبضات القديمة بنجاح', 'info')
    return redirect(url_for('telemetry'))


@app.route('/audit-logs')
@login_required
def audit_logs():
    logs = query_all("SELECT * FROM master_audit_logs ORDER BY id DESC LIMIT 150")
    return render_template('audit_logs.html', logs=logs)


@app.route('/audit-logs/clear', methods=['POST'])
@login_required
def audit_logs_clear():
    execute_write("DELETE FROM master_audit_logs")
    admin = session.get('master_admin', 'admin')
    log_audit('AUDIT_LOGS_CLEAR', 'system', None, "مسح سجلات التدقيق والأمان", admin, request.remote_addr)
    flash('تم مسح سجلات التدقيق والأمان بنجاح', 'info')
    return redirect(url_for('audit_logs'))


@app.route('/keys')
@login_required
def keys_management():
    pub_key = get_public_key_pem()
    key_fingerprint = hashlib.sha256(pub_key.encode('utf-8')).hexdigest()[:32].upper()
    return render_template('keys.html', public_key=pub_key, public_key_pem=pub_key, key_fingerprint=key_fingerprint)


@app.route('/api/v1/public-key', methods=['GET'])
def api_public_key():
    pub_key = get_public_key_pem()
    if request.args.get('format') == 'text':
        return pub_key, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    return jsonify({
        "status": "success",
        "algorithm": "Ed25519",
        "public_key_pem": pub_key,
        "fingerprint": hashlib.sha256(pub_key.encode('utf-8')).hexdigest()[:32].upper()
    }), 200


# ----------------- OTA Software Releases & Updates -----------------
@app.route('/updates', methods=['GET'])
@login_required
def updates_management():
    releases = query_all("SELECT * FROM master_updates ORDER BY id DESC")
    target_version = get_setting('latest_recommended_version', 'v3.0.0')
    return render_template('updates.html', releases=releases, target_version=target_version)


@app.route('/updates/publish', methods=['POST'])
@login_required
def updates_publish():
    version_tag = request.form.get('version_tag', '').strip()
    title = request.form.get('title', '').strip()
    changelog = request.form.get('changelog', '').strip()
    download_url = request.form.get('download_url', '').strip()
    is_mandatory = 1 if request.form.get('is_mandatory') == '1' else 0
    
    if version_tag and title:
        admin = session.get('master_admin', 'admin')
        execute_write("""
        INSERT INTO master_updates (version_tag, title, changelog, download_url, is_mandatory, status)
        VALUES (?, ?, ?, ?, ?, 'published')
        ON CONFLICT(version_tag) DO UPDATE SET
            title=excluded.title, changelog=excluded.changelog, download_url=excluded.download_url,
            is_mandatory=excluded.is_mandatory, status='published'
        """, (version_tag, title, changelog, download_url, is_mandatory))
        
        set_setting('latest_recommended_version', version_tag)
        log_audit('UPDATE_PUBLISH', 'update', version_tag, f"نشر إصدار برمجي جديد: {version_tag} ({title})", admin, request.remote_addr)
        flash(f"تم نشر الإصدار البرمجي {version_tag} بنجاح 🚀", 'success')
    return redirect(url_for('updates_management'))


@app.route('/updates/<int:update_id>/dispatch', methods=['POST'])
@login_required
def updates_dispatch(update_id):
    rel = query_one("SELECT * FROM master_updates WHERE id = ?", (update_id,))
    if rel:
        admin = session.get('master_admin', 'admin')
        target = request.form.get('target', 'all_active')
        
        payload_data = json.dumps({
            "version": rel['version_tag'],
            "download_url": rel['download_url'],
            "is_mandatory": bool(rel['is_mandatory'])
        })
        
        if target == 'all_active':
            execute_write(
                "UPDATE master_instances SET remote_action = 'update_software', remote_action_payload = ? WHERE status = 'active'",
                (payload_data,)
            )
            flash(f"تم إرسال أمر التحديث عبر الهواء للإصدار {rel['version_tag']} إلى جميع النسخ المرخصة والنشطة ⚡", 'success')
        else:
            execute_write(
                "UPDATE master_instances SET remote_action = 'update_software', remote_action_payload = ?",
                (payload_data,)
            )
            flash(f"تم إرسال أمر التحديث عبر الهواء للإصدار {rel['version_tag']} إلى كافة السيرفرات المتصلة", 'success')
            
        log_audit('UPDATE_DISPATCH', 'update', rel['version_tag'], f"إرسال أمر التحديث الهوائي للإصدار {rel['version_tag']}", admin, request.remote_addr)
    return redirect(url_for('updates_management'))


@app.route('/updates/<int:update_id>/delete', methods=['POST'])
@login_required
def updates_delete(update_id):
    rel = query_one("SELECT * FROM master_updates WHERE id = ?", (update_id,))
    if rel:
        execute_write("DELETE FROM master_updates WHERE id = ?", (update_id,))
        admin = session.get('master_admin', 'admin')
        log_audit('UPDATE_DELETE', 'update', rel['version_tag'], f"حذف سجل الإصدار: {rel['version_tag']}", admin, request.remote_addr)
        flash('تم حذف سجل الإصدار بنجاح', 'info')
    return redirect(url_for('updates_management'))


# ----------------- Security Blacklist Manager -----------------
@app.route('/blacklist', methods=['GET'])
@login_required
def blacklist_management():
    items = query_all("SELECT * FROM master_blacklist ORDER BY id DESC")
    return render_template('blacklist.html', items=items)


@app.route('/blacklist/add', methods=['POST'])
@login_required
def blacklist_add():
    target_type = request.form.get('target_type', 'ip').strip()
    target_value = request.form.get('target_value', '').strip()
    reason = request.form.get('reason', 'حظر أمني يدوي').strip()
    
    if target_value:
        admin = session.get('master_admin', 'admin')
        execute_write("""
        INSERT INTO master_blacklist (target_type, target_value, reason)
        VALUES (?, ?, ?)
        """, (target_type, target_value, reason))
        
        # If blocking an instance, also revoke its status
        if target_type == 'uuid':
            execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke' WHERE instance_uuid = ?", (target_value,))
        elif target_type == 'hwid':
            execute_write("UPDATE master_instances SET status = 'revoked', remote_action = 'revoke' WHERE server_hwid = ?", (target_value,))
            
        log_audit('BLACKLIST_ADD', 'blacklist', target_value, f"إضافة {target_type} إلى القائمة السوداء: {target_value} ({reason})", admin, request.remote_addr)
        flash(f"تمت إضافة ({target_value}) إلى القائمة السوداء وحظر وصوله نهائياً 🚫", 'danger')
    return redirect(url_for('blacklist_management'))


@app.route('/blacklist/<int:item_id>/delete', methods=['POST'])
@login_required
def blacklist_delete(item_id):
    item = query_one("SELECT * FROM master_blacklist WHERE id = ?", (item_id,))
    if item:
        execute_write("DELETE FROM master_blacklist WHERE id = ?", (item_id,))
        admin = session.get('master_admin', 'admin')
        log_audit('BLACKLIST_REMOVE', 'blacklist', item['target_value'], f"فك الحظر عن: {item['target_value']}", admin, request.remote_addr)
        flash(f"تم فك الحظر عن ({item['target_value']}) بنجاح", 'success')
    return redirect(url_for('blacklist_management'))


# ----------------- Database Backups & Recovery -----------------
@app.route('/backups', methods=['GET'])
@login_required
def backups_management():
    backups = query_all("SELECT * FROM master_backups ORDER BY id DESC")
    clients = query_all("SELECT id, name, network_name FROM master_clients ORDER BY name ASC")
    schedule = get_backup_schedule_settings()
    return render_template('backups.html', backups=backups, clients=clients, schedule=schedule)


@app.route('/backups/create', methods=['POST'])
@login_required
def backups_create():
    res = create_database_backup(backup_type='manual')
    flash(f"تم إنشاء نسخة احتياطية كاملة للنظام بنجاح: {res['filename']} ({res['size_kb']} KB) 💾", 'success')
    return redirect(url_for('backups_management'))


@app.route('/backups/client-create', methods=['POST'])
@login_required
def backups_client_create():
    client_id = request.form.get('client_id')
    if not client_id or not client_id.isdigit():
        flash("يرجى اختيار العميل لإنشاء نسخة احتياطية خاصة به ⚠️", "warning")
        return redirect(url_for('backups_management'))
    try:
        res = create_client_backup(int(client_id))
        flash(f"تم تصدير نسخة احتياطية للعميل ({res['client_name']}) بنجاح: {res['filename']} ({res['size_kb']} KB) 📦", "success")
    except Exception as e:
        flash(f"تعذر إنشاء نسخة العميل: {e}", "danger")
    return redirect(url_for('backups_management'))


@app.route('/backups/schedule/save', methods=['POST'])
@login_required
def backups_schedule_save():
    enabled = request.form.get('auto_backup_enabled') == '1'
    freq = request.form.get('auto_backup_frequency', 'daily')
    retention = int(request.form.get('auto_backup_retention', '14'))
    save_backup_schedule_settings(enabled, freq, retention)
    admin = session.get('master_admin', 'admin')
    status_text = "مفعل" if enabled else "معطل"
    log_audit('BACKUP_SCHEDULE_SAVE', 'backup', None, f"تحديث إعدادات جدولة النسخ الاحتياطي: {status_text}، التكرار: {freq}، الاحتفاظ: {retention}", admin, request.remote_addr)
    flash("تم حفظ إعدادات الجدولة والنسخ الاحتياطي التلقائي بنجاح 🟢", "success")
    return redirect(url_for('backups_management'))


@app.route('/backups/schedule/run-now', methods=['POST'])
@login_required
def backups_schedule_run_now():
    res = create_database_backup(backup_type='auto_scheduled', notes='تشغيل فوري لاختبار الجدولة')
    admin = session.get('master_admin', 'admin')
    log_audit('BACKUP_SCHEDULE_RUN_NOW', 'backup', res['id'], f"تشغيل فوري للنسخ المجدول: {res['filename']}", admin, request.remote_addr)
    flash(f"تم تنفيذ النسخ التلقائي المجدول بنجاح: {res['filename']} ({res['size_kb']} KB) 🚀", "success")
    return redirect(url_for('backups_management'))


@app.route('/backups/<int:backup_id>/download', methods=['GET'])
@login_required
def backups_download(backup_id):
    path, filename = get_backup_file(backup_id)
    if not path or not os.path.exists(path):
        flash('ملف النسخة الاحتياطية غير موجود أو تم حذفه', 'danger')
        return redirect(url_for('backups_management'))
        
    mimetype = 'application/json' if filename.endswith('.json') else 'application/x-sqlite3'
    return send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype
    )


@app.route('/backups/<int:backup_id>/delete', methods=['POST'])
@login_required
def backups_delete(backup_id):
    if delete_backup(backup_id):
        admin = session.get('master_admin', 'admin')
        log_audit('BACKUP_DELETE', 'backup', backup_id, f"حذف ملف النسخة الاحتياطية رقم {backup_id}", admin, request.remote_addr)
        flash('تم حذف ملف النسخة الاحتياطية بنجاح', 'info')
    return redirect(url_for('backups_management'))


# ----------------- Settings & Alerts Configuration -----------------
@app.route('/settings', methods=['GET'])
@login_required
def settings_page():
    all_settings = query_all("SELECT * FROM master_system_settings")
    settings_dict = {s['key']: s['value'] for s in all_settings}
    return render_template('settings.html', settings=settings_dict)


@app.route('/settings/save', methods=['POST'])
@login_required
def settings_save():
    keys = [
        "telegram_enabled", "telegram_bot_token", "telegram_chat_id",
        "alert_on_rogue_instance", "alert_on_hwid_mismatch", "alert_on_expiring_soon",
        "whatsapp_enabled", "whatsapp_api_url", "whatsapp_api_key",
        "auto_backup_enabled", "trial_default_days", "trial_default_subs", "trial_default_nas"
    ]
    for k in keys:
        if k in ["telegram_enabled", "alert_on_rogue_instance", "alert_on_hwid_mismatch", "alert_on_expiring_soon", "whatsapp_enabled", "auto_backup_enabled"]:
            val = "1" if request.form.get(k) == "1" else "0"
        else:
            val = request.form.get(k, '').strip()
        set_setting(k, val)
        
    admin = session.get('master_admin', 'admin')
    log_audit('SETTINGS_UPDATE', 'system', None, "تحديث إعدادات النظام والتنبيهات", admin, request.remote_addr)
    flash('تم حفظ وتحديث إعدادات النظام والتنبيهات بنجاح ⚙️', 'success')
    return redirect(url_for('settings_page'))


@app.route('/settings/test-telegram', methods=['POST'])
@login_required
def settings_test_telegram():
    token = request.form.get('bot_token', '').strip() or get_setting('telegram_bot_token', '')
    chat_id = request.form.get('chat_id', '').strip() or get_setting('telegram_chat_id', '')
    
    if not token or not chat_id:
        return jsonify({"success": False, "message": "يرجى إدخال Bot Token و Chat ID أولاً"}), 400
        
    ok, msg = test_telegram_connection(token, chat_id)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)


# ----------------- Analytics & Charts JSON API -----------------
@app.route('/api/v1/analytics/stats', methods=['GET'])
@login_required
def api_analytics_stats():
    # 1. Total Subscribers, Active Sessions & NAS across all instances
    totals = query_one("""
        SELECT 
            COALESCE(SUM(active_sessions_count), SUM(subscribers_count), 0) as total_sessions,
            COALESCE(SUM(subscribers_count), 0) as total_subs, 
            COALESCE(SUM(nas_count), 0) as total_nas, 
            COUNT(*) as total_instances 
        FROM master_instances
    """)
    
    # 2. Plan Tier Distribution
    tiers = query_all("SELECT plan_tier, COUNT(*) as count FROM master_licenses GROUP BY plan_tier")
    tier_labels = [t['plan_tier'] for t in tiers]
    tier_counts = [t['count'] for t in tiers]
    
    # 3. License Status Breakdown
    statuses = query_all("SELECT status, COUNT(*) as count FROM master_licenses GROUP BY status")
    status_map = {s['status']: s['count'] for s in statuses}
    
    # 4. Top 5 Largest Networks
    top_networks = query_all("""
        SELECT client_name, active_sessions_count, subscribers_count, nas_count, app_version, status, last_ping_at 
        FROM master_instances 
        WHERE client_name IS NOT NULL AND client_name != ''
        ORDER BY COALESCE(active_sessions_count, subscribers_count, 0) DESC LIMIT 5
    """)
    
    return jsonify({
        "status": "success",
        "total_active_sessions": totals['total_sessions'] or 0,
        "total_subscribers": totals['total_subs'] or 0,
        "total_nas": totals['total_nas'] or 0,
        "total_instances": totals['total_instances'] or 0,
        "tiers": {
            "labels": tier_labels,
            "counts": tier_counts
        },
        "license_statuses": {
            "active": status_map.get('active', 0),
            "suspended": status_map.get('suspended', 0),
            "revoked": status_map.get('revoked', 0),
            "expired": status_map.get('expired', 0)
        },
        "top_networks": top_networks
    }), 200


# ----------------- REST API for Clients (Heartbeat & Activation) -----------------
@app.route('/api/v1/heartbeat/ping', methods=['POST'])
def api_heartbeat_ping():
    """
    Central Telemetry & Heartbeat Endpoint:
    Auto-discovers instances, checks blacklist, validates licenses, enforces quotas, and delivers remote control commands.
    """
    try:
        data = g.verified_license_request
    except Exception:
        data = {}
        
    license_id = data.get('license_id', '').strip()
    hardware_id = data.get('hardware_id', '').strip()
    instance_uuid = data.get('instance_uuid', '').strip()
    app_version = data.get('app_version', 'v3.0')
    active_value = data.get('current_active_sessions')
    active_sessions_count = int(active_value) if active_value is not None and int(active_value) >= 0 else None
    subs_count = int(data.get('subscribers_count', 0))
    nas_count = int(data.get('nas_count', 0))
    system_uptime = data.get('system_uptime', 'Online')
    domain = data.get('domain', request.host)
    client_ip = request.remote_addr or ''
    
    # Auto-generate instance_uuid if not supplied by legacy clients
    if not instance_uuid:
        instance_uuid = hashlib.sha256(f"{client_ip}_{hardware_id}_{license_id}".encode()).hexdigest()[:16].upper()
        
    # Security Check: Blacklist Filter
    blacklisted, b_reason = is_blacklisted(ip=client_ip, hwid=hardware_id, uuid=instance_uuid)
    if blacklisted:
        return jsonify({
            "status": "blacklisted",
            "valid": False,
            "message": f"تم حظر هذا السيرفر أمنياً من الوصول إلى خادم التراخيص ({b_reason})",
            "remote_action": "revoke",
            "remote_payload": None
        }), 403
        
    # 1. Lookup License
    lic = query_one("SELECT * FROM master_licenses WHERE license_id = ?", (license_id,)) if license_id else None
    
    status = 'unlicensed'
    client_name = 'نسخة مكتشفة غير مرخصة'
    is_valid = False
    message = "النسخة غير مفعلة أو لا يوجد ترخيص مسجل"
    remote_action = 'none'
    remote_payload = None
    
    if not lic:
        if license_id:
            status = 'revoked'
            is_valid = False
            message = "الترخيص غير موجود أو تم حذفه من قِبل إدارة المطور 🔴"
            remote_action = 'revoke'
    else:
        client_name = lic['client_name']
        
        # Check license status
        if lic['status'] == 'revoked':
            status = 'revoked'
            is_valid = False
            remote_action = 'revoke'
            message = f"تم إلغاء وحظر هذا الترخيص نهائياً ({lic.get('revocation_reason', '')})"
        elif lic['status'] == 'suspended':
            status = 'suspended'
            is_valid = False
            remote_action = 'suspend'
            message = "تم تعليق هذا الترخيص مؤقتاً لمراجعة الإدارة ⏸️"
        else:
            # Check expiry date
            now = datetime.datetime.utcnow()
            exp_dt = parse_flexible_datetime(lic['expires_at'])
            if exp_dt < now:
                status = 'expired'
                is_valid = False
                message = f"انتهت صلاحية هذا الترخيص بتاريخ {lic['expires_at'][:10]}"
            else:
                # Check Hardware ID binding
                lic_hw = lic['hardware_id'].strip().upper()
                if lic_hw not in ('ANY', '*', 'ALL', 'UNLOCKED', ''):
                    clean_lic_hw = lic_hw.replace('-', '')
                    clean_cur_hw = hardware_id.replace('-', '').strip().upper()
                    if clean_lic_hw != clean_cur_hw:
                        status = 'hwid_mismatch'
                        is_valid = False
                        message = f"هذا الترخيص مقفل على سيرفر آخر ({lic_hw})"
                        notify_hwid_mismatch(lic['license_id'], lic_hw, hardware_id, client_ip, lic.get('client_name'))
                    else:
                        status = 'active'
                        is_valid = True
                        message = "الترخيص معتمد وصالح وموثق رقمياً 🟢"
                else:
                    status = 'active'
                    is_valid = True
                    message = "الترخيص معتمد وصالح وموثق رقمياً 🟢"
                    
                lic_instance = (lic.get('instance_id') or 'ANY').strip().upper()
                if is_valid and lic_instance not in ('ANY','*','ALL','UNLOCKED','') and lic_instance.replace('-','') != instance_uuid.upper().replace('-',''):
                    status, is_valid, message = 'hwid_mismatch', False, 'Instance binding mismatch'
                if is_valid:
                    instance_control = query_one('SELECT status FROM master_instances WHERE instance_uuid=?', (instance_uuid,))
                    if instance_control and instance_control['status'] in ('revoked','suspended'):
                        status, is_valid, message = instance_control['status'], False, 'Instance disabled by administrator'
                    elif not claim_installation(license_id, instance_uuid, int(lic.get('max_instances',1))):
                        status, is_valid, message = 'instance_limit', False, 'Licensed installation capacity reached'

                # Check active sessions quota limits
                max_active = lic.get('max_active_sessions')
                max_subs = lic.get('max_subscribers', 0)
                
                if is_valid and active_sessions_count is not None and max_active and int(max_active) > 0:
                    if active_sessions_count >= int(max_active):
                        status = 'over_quota'
                        is_valid = True
                        message = f"تنبيه: بلوغ الحد الأقصى للجلسات المتصلة المتزامنة في باقة الترخيص ({active_sessions_count:,} / {int(max_active):,}) ⚠️"
                        try:
                            send_telegram_alert(f"⚠️ <b>تنبيه: بلوغ سعة الجلسات المتصلة</b>\n\n"
                                                f"العميل: <b>{client_name}</b>\n"
                                                f"الترخيص: <code>{license_id}</code>\n"
                                                f"الجلسات المتصلة الحالية: <b>{active_sessions_count:,}</b>\n"
                                                f"سقف الباقة المرخص: <b>{int(max_active):,}</b> جلسة\n"
                                                f"عنوان الـ IP: <code>{client_ip}</code>\n"
                                                f"الحالة: حظر الاتصالات الجديدة فقط مع استمرار الجلسات القائمة ولوحة التحكم.")
                        except Exception:
                            pass
        # Update last heartbeat in master_licenses
        execute_write("UPDATE master_licenses SET last_heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?", (lic['id'],))
        
    # 2. Upsert into master_instances (Auto-Discovery)
    # Deleted licenses cannot be stored in legacy telemetry tables with a foreign key.
    stored_license_id = lic['license_id'] if lic else None
    existing_inst = query_one("SELECT * FROM master_instances WHERE instance_uuid = ?", (instance_uuid,))
    preserve_existing_instance = False
    if existing_inst and not is_valid and existing_inst.get('license_id') != license_id:
        # An obsolete client's heartbeat must not replace a newer assigned license
        # or consume the command intended for that license.
        assigned_license_id = existing_inst.get('license_id')
        if assigned_license_id:
            preserve_existing_instance = bool(query_one(
                "SELECT id FROM master_licenses WHERE license_id = ?", (assigned_license_id,)
            ))
    if existing_inst and not preserve_existing_instance:
        inst_remote_action = existing_inst.get('remote_action', 'none')
        if inst_remote_action and inst_remote_action != 'none':
            remote_action = inst_remote_action
        remote_payload = existing_inst.get('remote_action_payload')
        
        # If admin explicitly suspended or revoked this specific instance
        if existing_inst['status'] in ('suspended', 'revoked'):
            status = existing_inst['status']
            is_valid = False
            remote_action = 'suspend' if status == 'suspended' else 'revoke'
            message = f"تم تطبيق أمر {status} على هذه النسخة مباشرة من الإدارة"
            
        execute_write("""
        UPDATE master_instances SET
            server_hwid = ?, license_id = ?, client_name = ?, client_ip = ?, domain = ?,
            app_version = ?, status = ?, active_sessions_count = ?, subscribers_count = ?, nas_count = ?,
            system_uptime = ?, last_ping_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (
            hardware_id, stored_license_id, client_name, client_ip, domain,
            app_version, status, active_sessions_count, subs_count, nas_count, system_uptime, existing_inst['id']
        ))
        
        # Reset remote action once consumed
        if remote_action != 'none':
            execute_write("UPDATE master_instances SET remote_action = 'none' WHERE id = ?", (existing_inst['id'],))
    elif not existing_inst:
        execute_write("""
        INSERT INTO master_instances (
            instance_uuid, server_hwid, license_id, client_name, client_ip, domain,
            app_version, status, active_sessions_count, subscribers_count, nas_count, system_uptime,
            first_seen_at, last_ping_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            instance_uuid, hardware_id, stored_license_id, client_name, client_ip, domain,
            app_version, status, active_sessions_count, subs_count, nas_count, system_uptime
        ))
        
        # Notify Admin on newly discovered rogue instance
        if not is_valid:
            notify_rogue_instance({
                "instance_uuid": instance_uuid,
                "hardware_id": hardware_id,
                "client_ip": client_ip,
                "app_version": app_version,
                "active_sessions_count": active_sessions_count,
                "subscribers_count": subs_count,
                "nas_count": nas_count
            })
            
    # 3. Record Telemetry entry
    execute_write("""
    INSERT INTO master_telemetry (
        license_id, instance_uuid, client_ip, hardware_id, app_version,
        active_sessions_count, subscribers_count, nas_count, system_uptime, status, pinged_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (stored_license_id, instance_uuid, client_ip, hardware_id, app_version, active_sessions_count, subs_count, nas_count, system_uptime, status))
    
    response_payload = {
        "status": status,
        "valid": is_valid,
        "message": message,
        "license_id": license_id,
        "server_time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "remote_action": remote_action,
        "remote_payload": remote_payload
    }
    
    return jsonify(response_payload), 200


def backup_scheduler_worker():
    """Background worker that continuously evaluates scheduled backups."""
    import time
    time.sleep(10)  # Initial grace delay upon server start
    while True:
        try:
            check_and_run_scheduled_backup()
        except Exception:
            pass
        time.sleep(60)


_scheduler_thread = threading.Thread(target=backup_scheduler_worker, daemon=True)
_scheduler_thread.start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3030))
    app.run(host=os.environ.get('BIND_HOST', '0.0.0.0'), port=port, debug=False)

