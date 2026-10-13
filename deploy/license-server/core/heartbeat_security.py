"""Administrator-enrolled instance identities and replay-safe license responses."""
import base64
import hashlib
import time
from flask import request, g, abort
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from core.license_protocol import canonical
from core.crypto_engine import sign_data_payload
from database.db import get_db
from core.instance_enrollment import initialize_enrollment, enroll_in_transaction, EnrollmentError


def initialize_protocol():
    conn = get_db()
    try:
        with conn:
            initialize_enrollment(conn)
            conn.execute('CREATE TABLE IF NOT EXISTS master_instance_keys (instance_uuid TEXT PRIMARY KEY, public_key TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS master_request_nonces (instance_uuid TEXT NOT NULL, nonce TEXT NOT NULL, received_epoch INTEGER NOT NULL, PRIMARY KEY(instance_uuid,nonce))')
            conn.execute('CREATE TABLE IF NOT EXISTS master_license_installations (license_id TEXT NOT NULL REFERENCES master_licenses(license_id) ON DELETE CASCADE, instance_uuid TEXT NOT NULL, PRIMARY KEY(license_id,instance_uuid))')
    finally:
        conn.close()


def register_key(instance_uuid, public_key):
    instance_uuid = instance_uuid.strip().upper()
    public_key = public_key.strip()
    if len(instance_uuid) < 8 or len(instance_uuid) > 100:
        raise ValueError('Invalid instance identifier')
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    conn = get_db()
    try:
        with conn:
            conn.execute('INSERT INTO master_instance_keys VALUES (?,?) ON CONFLICT(instance_uuid) DO UPDATE SET public_key=excluded.public_key', (instance_uuid,public_key))
            # Retain recent nonces across key rotation, so rotation cannot authorize a replay.
    finally:
        conn.close()


def claim_installation(license_id, instance_uuid, limit):
    if type(limit) is not int or limit < 0:
        return False
    conn = get_db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            licensed_instance = conn.execute('SELECT instance_id FROM master_licenses WHERE license_id=?',(license_id,)).fetchone()
            if licensed_instance:
                assigned = (licensed_instance['instance_id'] or 'ANY').upper()
                if assigned not in ('ANY','*','ALL','UNLOCKED',''):
                    conn.execute('DELETE FROM master_license_installations WHERE license_id=? AND instance_uuid<>?',(license_id,instance_uuid))
            existing = conn.execute('SELECT 1 FROM master_license_installations WHERE license_id=? AND instance_uuid=?',(license_id,instance_uuid)).fetchone()
            if existing:
                return True
            count = conn.execute('SELECT COUNT(*) FROM master_license_installations WHERE license_id=?',(license_id,)).fetchone()[0]
            if limit and count >= limit:
                return False
            conn.execute('INSERT INTO master_license_installations VALUES(?,?)',(license_id,instance_uuid))
            return True
    finally:
        conn.close()


def install_protocol(app):
    initialize_protocol()
    @app.before_request
    def verify_instance_request():
        if request.path != '/api/v1/heartbeat/ping':
            return None
        if not request.is_secure and request.remote_addr not in ('127.0.0.1','::1'):
            abort(400, 'HTTPS required')
        if request.content_length is None or request.content_length > 1048576:
            abort(400, 'Invalid request size')
        envelope = request.get_json(silent=True)
        if not isinstance(envelope,dict) or not isinstance(envelope.get('payload'),dict):
            abort(401, 'Signed instance request required')
        payload = envelope['payload']
        instance = payload.get('instance_uuid')
        nonce = payload.get('request_nonce')
        epoch = payload.get('request_epoch')
        if not isinstance(instance,str) or not isinstance(nonce,str) or len(nonce)!=48 or type(epoch) is not int or abs(time.time()-epoch)>120:
            abort(401, 'Invalid or stale instance request')
        conn = get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT public_key FROM master_instance_keys WHERE instance_uuid=?',(instance,)).fetchone()
            if row and envelope.get('public_key') != row['public_key']:
                from flask import jsonify, make_response
                abort(make_response(jsonify(error='enrollment_key_mismatch'), 401))
            try:
                public = row['public_key'] if row else envelope.get('public_key', '')
                key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public,validate=True))
                key.verify(base64.b64decode(envelope['signature'],validate=True),canonical(payload))
                if not row:
                    enroll_in_transaction(conn, payload, public, request.remote_addr or '')
                conn.execute('DELETE FROM master_request_nonces WHERE received_epoch<?',(int(time.time())-300,))
                conn.execute('INSERT INTO master_request_nonces VALUES(?,?,?)',(instance,nonce,int(time.time())))
            except EnrollmentError as exc:
                conn.rollback()
                from flask import jsonify, make_response
                abort(make_response(jsonify(error=str(exc)), 401))
            except Exception:
                conn.rollback()
                abort(401, 'Invalid signature or replayed request')
            conn.commit()
        finally:
            conn.close()
        g.verified_license_request = payload
    @app.after_request
    def sign_license_response(response):
        payload = getattr(g,'verified_license_request',None)
        if payload is not None and response.status_code in (200,403) and response.is_json:
            body = response.get_json()
            body.update({k:payload.get(k) for k in ('license_id','hardware_id','instance_uuid','request_nonce')})
            body.update(request_hash=hashlib.sha256(canonical(payload)).hexdigest(),issued_epoch=int(time.time()),expires_epoch=int(time.time())+90)
            signature = base64.b64encode(sign_data_payload(canonical(body))).decode('ascii')
            from flask import jsonify
            signed = jsonify(payload=body,signature=signature)
            return signed
        return response
