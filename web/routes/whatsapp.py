# -*- coding: utf-8 -*-
"""
web/routes/whatsapp.py
----------------------
Flask Web Blueprint for WhatsApp Engine & Interactive Bot in MAX RADIUS.
"""

import json
import logging
import datetime
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash

from core.rbac import login_required, require_permission, get_current_manager
from services.whatsapp_service import (
    get_whatsapp_settings, update_whatsapp_settings,
    get_all_templates, get_template_by_key, save_template,
    get_whatsapp_logs, clear_whatsapp_logs, send_whatsapp_message,
    process_incoming_whatsapp_message, send_broadcast_campaign,
    get_evolution_qr_code, get_evolution_connection_state, restart_evolution_instance
)
from database.db import query_all, query_one

logger = logging.getLogger('whatsapp_bp')
whatsapp_bp = Blueprint('whatsapp_bp', __name__)


# --------------------------------------------------------------------------
# Web Pages
# --------------------------------------------------------------------------

@whatsapp_bp.route('/whatsapp/dashboard', methods=['GET'], endpoint='whatsapp_dashboard')
@login_required
@require_permission('settings.manage')
def whatsapp_dashboard():
    settings = get_whatsapp_settings()
    templates = get_all_templates()
    logs_summary = query_all("""
        SELECT status, COUNT(*) as cnt 
        FROM wisp_whatsapp_logs 
        GROUP BY status
    """)
    stats = {
        'total': sum(r['cnt'] for r in logs_summary) if logs_summary else 0,
        'sent': sum(r['cnt'] for r in logs_summary if r['status'] in ('sent', 'delivered')) if logs_summary else 0,
        'received': sum(r['cnt'] for r in logs_summary if r['status'] == 'received') if logs_summary else 0,
        'failed': sum(r['cnt'] for r in logs_summary if r['status'] == 'failed') if logs_summary else 0,
    }
    recent_logs = get_whatsapp_logs(limit=10)
    return render_template(
        'whatsapp/dashboard.html',
        settings=settings,
        templates=templates,
        stats=stats,
        recent_logs=recent_logs
    )


@whatsapp_bp.route('/whatsapp/templates', methods=['GET'], endpoint='whatsapp_templates_page')
@login_required
@require_permission('settings.manage')
def whatsapp_templates_page():
    templates = get_all_templates()
    return render_template('whatsapp/templates.html', templates=templates)


@whatsapp_bp.route('/whatsapp/broadcast', methods=['GET'], endpoint='whatsapp_broadcast_page')
@login_required
@require_permission('settings.manage')
def whatsapp_broadcast_page():
    resellers = query_all("SELECT id, name FROM wisp_resellers WHERE phone IS NOT NULL AND phone != '' ORDER BY name ASC")
    subscribers_count = query_one("SELECT COUNT(*) as cnt FROM wisp_subscribers WHERE phone IS NOT NULL AND phone != ''")
    sub_cnt = subscribers_count['cnt'] if subscribers_count else 0
    return render_template(
        'whatsapp/broadcast.html',
        resellers=resellers or [],
        subscribers_count=sub_cnt
    )


@whatsapp_bp.route('/whatsapp/logs', methods=['GET'], endpoint='whatsapp_logs_page')
@login_required
@require_permission('settings.manage')
def whatsapp_logs_page():
    status_filter = request.args.get('status', 'all')
    phone_filter = request.args.get('phone', '').strip()
    logs = get_whatsapp_logs(limit=200, filter_status=status_filter, filter_phone=phone_filter)
    return render_template(
        'whatsapp/logs.html',
        logs=logs,
        status_filter=status_filter,
        phone_filter=phone_filter
    )


@whatsapp_bp.route('/whatsapp/simulator', methods=['GET'], endpoint='whatsapp_simulator_page')
@login_required
@require_permission('settings.manage')
def whatsapp_simulator_page():
    settings = get_whatsapp_settings()
    sample_vouchers = query_all("SELECT username, pin_code, status FROM wisp_vouchers ORDER BY id DESC LIMIT 5")
    return render_template(
        'whatsapp/simulator.html',
        settings=settings,
        sample_vouchers=sample_vouchers or []
    )


# --------------------------------------------------------------------------
# AJAX API Endpoints
# --------------------------------------------------------------------------

@whatsapp_bp.route('/api/whatsapp/settings/save', methods=['POST'], endpoint='api_whatsapp_save_settings')
@login_required
@require_permission('settings.manage')
def api_whatsapp_save_settings():
    data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    success, msg = update_whatsapp_settings(data)
    return jsonify({'success': success, 'message': msg})


@whatsapp_bp.route('/api/whatsapp/templates/save', methods=['POST'], endpoint='api_whatsapp_save_template')
@login_required
@require_permission('settings.manage')
def api_whatsapp_save_template():
    data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    template_id = data.get('template_id')
    title = data.get('title', '').strip()
    template_text = data.get('template_text', '').strip()
    is_enabled = 1 if str(data.get('is_enabled', '1')).lower() in ('1', 'true', 'on') else 0

    if not template_id or not template_text:
        return jsonify({'success': False, 'message': 'بيانات القالب غير مكتملة.'})

    success, msg = save_template(template_id, title, template_text, is_enabled)
    return jsonify({'success': success, 'message': msg})


@whatsapp_bp.route('/api/whatsapp/send-test', methods=['POST'], endpoint='api_whatsapp_send_test')
@login_required
@require_permission('settings.manage')
def api_whatsapp_send_test():
    data = request.get_json(silent=True) or request.form.to_dict()
    phone = data.get('phone_number', '').strip()
    message = data.get('message', 'رسالة اختبارية من نظام MAX RADIUS 🚀').strip()
    if not phone:
        return jsonify({'success': False, 'message': 'يرجى إدخال رقم الهاتف المستلم.'})

    success, msg = send_whatsapp_message(phone, message, event_type='manual')
    return jsonify({'success': success, 'message': msg})


@whatsapp_bp.route('/api/whatsapp/broadcast/send', methods=['POST'], endpoint='api_whatsapp_send_broadcast')
@login_required
@require_permission('settings.manage')
def api_whatsapp_send_broadcast():
    data = request.get_json(silent=True) or request.form.to_dict()
    audience = data.get('audience', 'all_subscribers')
    message = data.get('message_text', '').strip()
    reseller_id = data.get('reseller_id')

    if not message:
        return jsonify({'success': False, 'message': 'يرجى كتابة نص الرسالة الجماعية.'})

    total, failed, msg = send_broadcast_campaign(audience, message, reseller_id)
    return jsonify({'success': (total > 0), 'total': total, 'message': msg})


@whatsapp_bp.route('/api/whatsapp/logs/clear', methods=['POST'], endpoint='api_whatsapp_clear_logs')
@login_required
@require_permission('settings.manage')
def api_whatsapp_clear_logs():
    success, msg = clear_whatsapp_logs()
    return jsonify({'success': success, 'message': msg})


@whatsapp_bp.route('/api/whatsapp/simulate-chat', methods=['POST'], endpoint='api_whatsapp_simulate_chat')
@login_required
@require_permission('settings.manage')
def api_whatsapp_simulate_chat():
    data = request.get_json(silent=True) or request.form.to_dict()
    phone = data.get('phone', '967777000000').strip()
    message = data.get('message', '').strip()
    sender_name = data.get('sender_name', 'مستخدم تجريبي').strip()

    if not message:
        return jsonify({'success': False, 'reply': 'الرسالة فارغة.'})

    reply = process_incoming_whatsapp_message(phone, message, sender_name)
    return jsonify({
        'success': True,
        'reply': reply or 'لم يتم تفعيل الرد التلقائي.',
        'timestamp': datetime.datetime.now().strftime('%H:%M')
    })


# --------------------------------------------------------------------------
# Incoming Webhook Endpoint (Evolution API / Meta Cloud API)
# --------------------------------------------------------------------------

@whatsapp_bp.route('/api/whatsapp/webhook', methods=['GET', 'POST'], endpoint='api_whatsapp_webhook')
def api_whatsapp_webhook():
    # 1. Meta Webhook Verification Challenge (GET)
    if request.method == 'GET':
        settings = get_whatsapp_settings()
        verify_token = settings.get('meta_webhook_verify_token', 'max_radius_whatsapp_token_2026')
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == verify_token:
            return challenge, 200
        return 'Forbidden', 403

    # 2. Incoming Message Payload (POST)
    payload = request.get_json(silent=True) or {}
    logger.info(f"Incoming WhatsApp webhook payload: {payload}")

    # Evolution API format
    if 'data' in payload and isinstance(payload['data'], dict):
        d = payload['data']
        key = d.get('key', {})
        if not key.get('fromMe', False):
            phone = key.get('remoteJid', '').split('@')[0]
            msg_obj = d.get('message', {})
            text = (
                msg_obj.get('conversation') or
                msg_obj.get('extendedTextMessage', {}).get('text') or
                ''
            )
            sender_name = d.get('pushName', '')
            if phone and text:
                reply = process_incoming_whatsapp_message(phone, text, sender_name)
                if reply:
                    send_whatsapp_message(phone, reply, event_type='bot_reply')

    # Meta Cloud API format
    elif 'entry' in payload and isinstance(payload['entry'], list):
        for entry in payload['entry']:
            for change in entry.get('changes', []):
                val = change.get('value', {})
                for msg in val.get('messages', []):
                    phone = msg.get('from', '')
                    text = msg.get('text', {}).get('body', '')
                    contact_name = ''
                    for c in val.get('contacts', []):
                        if c.get('wa_id') == phone:
                            contact_name = c.get('profile', {}).get('name', '')
                    if phone and text:
                        reply = process_incoming_whatsapp_message(phone, text, contact_name)
                        if reply:
                            send_whatsapp_message(phone, reply, event_type='bot_reply')

    # Generic simple webhook format {"phone": "...", "message": "..."}
    elif 'phone' in payload and 'message' in payload:
        phone = payload.get('phone')
        text = payload.get('message')
        reply = process_incoming_whatsapp_message(phone, text)
        if reply:
            send_whatsapp_message(phone, reply, event_type='bot_reply')

    return jsonify({'status': 'ok'}), 200


@whatsapp_bp.route('/api/whatsapp/qrcode', methods=['GET'], endpoint='api_whatsapp_qrcode')
@login_required
@require_permission('settings.manage')
def api_whatsapp_qrcode():
    """Fetches live QR Code base64 image and pairing code from Evolution API."""
    res = get_evolution_qr_code()
    return jsonify(res)


@whatsapp_bp.route('/api/whatsapp/sync-status', methods=['GET'], endpoint='api_whatsapp_sync_status')
@login_required
@require_permission('settings.manage')
def api_whatsapp_sync_status():
    """Fetches current live connection status from Evolution API."""
    res = get_evolution_connection_state()
    return jsonify(res)


@whatsapp_bp.route('/api/whatsapp/restart-instance', methods=['POST'], endpoint='api_whatsapp_restart_instance')
@login_required
@require_permission('settings.manage')
def api_whatsapp_restart_instance():
    """Restarts Evolution instance."""
    res = restart_evolution_instance()
    return jsonify(res)
