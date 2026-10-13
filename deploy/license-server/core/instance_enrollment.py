"""Single-use enrollment capabilities; no automatic replacement of trusted keys."""
import base64
import datetime
import hashlib
import json
import secrets
import time
from database.db import get_db


class EnrollmentError(ValueError):
    pass


def initialize_enrollment(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS master_enrollment_tokens (
        token_hash TEXT PRIMARY KEY, license_id TEXT NOT NULL,
        issued_epoch INTEGER NOT NULL, expires_epoch INTEGER NOT NULL,
        consumed_instance TEXT, consumed_key_hash TEXT,
        FOREIGN KEY(license_id) REFERENCES master_licenses(license_id) ON DELETE CASCADE)''')


def active_license(lic):
    if not lic or lic['status'] != 'active':
        raise EnrollmentError('enrollment_license_unavailable')
    try:
        expiry = datetime.datetime.fromisoformat(lic['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        if expiry.timestamp() <= time.time():
            raise ValueError('expired')
    except (ValueError, TypeError, AttributeError):
        raise EnrollmentError('enrollment_license_unavailable')


def activation_bundle(license_id):
    """Called only from authenticated issuer administration, never public discovery."""
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        lic = conn.execute('SELECT * FROM master_licenses WHERE license_id=?', (license_id,)).fetchone()
        active_license(lic)
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        conn.execute('DELETE FROM master_enrollment_tokens WHERE expires_epoch<?', (now,))
        conn.execute('INSERT INTO master_enrollment_tokens VALUES (?,?,?,?,NULL,NULL)',
                     (hashlib.sha256(token.encode()).hexdigest(), license_id, now, now + 86400))
        conn.execute('''INSERT INTO master_audit_logs(action,entity_type,entity_id,details)
                        VALUES ('ENROLLMENT_CAPABILITY_ISSUED','license',?,'24-hour single-use activation capability')''', (license_id,))
        package = json.loads(lic['raw_json'])
        package['enrollment_token'] = token
        raw = json.dumps(package, ensure_ascii=False)
        conn.commit()
        return {'raw_json': raw, 'token_b64': base64.b64encode(raw.encode()).decode()}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enroll_in_transaction(conn, payload, public_key, ip):
    """Caller holds BEGIN IMMEDIATE and verifies possession before entering here."""
    token = payload.get('enrollment_token')
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise EnrollmentError('enrollment_required')
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    permit = conn.execute('SELECT * FROM master_enrollment_tokens WHERE token_hash=?', (token_hash,)).fetchone()
    license_id = payload.get('license_id')
    if not permit or permit['license_id'] != license_id or permit['consumed_instance'] is not None:
        raise EnrollmentError('enrollment_token_invalid')
    if permit['expires_epoch'] <= time.time():
        raise EnrollmentError('enrollment_token_expired')
    lic = conn.execute('SELECT * FROM master_licenses WHERE license_id=?', (license_id,)).fetchone()
    active_license(lic)
    instance = payload['instance_uuid']
    hardware = payload.get('hardware_id', '')
    if not isinstance(hardware, str) or not hardware or not 8 <= len(instance) <= 100:
        raise EnrollmentError('enrollment_binding_mismatch')
    for required, actual in [(lic['hardware_id'], hardware), (lic['instance_id'], instance)]:
        binding = (required or 'ANY').strip().upper()
        if binding not in ('ANY', '*', 'ALL', 'UNLOCKED', '') and binding.replace('-', '') != actual.strip().upper().replace('-', ''):
            raise EnrollmentError('enrollment_binding_mismatch')
    blocked = conn.execute('''SELECT 1 FROM master_blacklist WHERE
        (target_type='ip' AND target_value=?) OR (target_type='hwid' AND target_value=?)
        OR (target_type='uuid' AND target_value=?)''', (ip, hardware.strip().upper(), instance.upper())).fetchone()
    controlled = conn.execute('SELECT status FROM master_instances WHERE instance_uuid=?', (instance,)).fetchone()
    if blocked or (controlled and controlled['status'] in ('revoked', 'suspended')):
        raise EnrollmentError('enrollment_blocked')
    limit = lic['max_instances']
    if type(limit) is not int or limit < 0:
        raise EnrollmentError('enrollment_license_unavailable')
    existing = conn.execute('SELECT 1 FROM master_license_installations WHERE license_id=? AND instance_uuid=?', (license_id, instance)).fetchone()
    if (lic['instance_id'] or 'ANY').strip().upper() not in ('ANY', '*', 'ALL', 'UNLOCKED', ''):
        # Preserve the existing issuer-authorized reassignment semantics atomically.
        conn.execute('DELETE FROM master_license_installations WHERE license_id=? AND instance_uuid<>?', (license_id, instance))
    count = conn.execute('SELECT COUNT(*) FROM master_license_installations WHERE license_id=?', (license_id,)).fetchone()[0]
    if not existing and limit and count >= limit:
        raise EnrollmentError('enrollment_instance_limit')
    # Key, reservation, token consumption, audit and request nonce share the caller's transaction.
    conn.execute('INSERT INTO master_instance_keys VALUES (?,?)', (instance, public_key))
    conn.execute('INSERT OR IGNORE INTO master_license_installations VALUES (?,?)', (license_id, instance))
    conn.execute('UPDATE master_enrollment_tokens SET consumed_instance=?,consumed_key_hash=? WHERE token_hash=?',
                 (instance, hashlib.sha256(public_key.encode()).hexdigest(), token_hash))
    conn.execute('''INSERT INTO master_audit_logs(action,entity_type,entity_id,details)
                    VALUES ('INSTANCE_KEY_REGISTERED','instances',?,'Verified single-use activation capability')''', (instance,))
