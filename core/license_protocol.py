"""Mutually authenticated license messages; signing keys never leave the instance."""
import base64
import datetime
import hashlib
import json
import os
import secrets
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def instance_key(storage):
    path = Path(storage) / 'secrets' / 'license-instance.pem'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        key = Ed25519PrivateKey.generate()
        with os.fdopen(fd, 'wb') as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    for _ in range(20):
        try:
            return serialization.load_pem_private_key(path.read_bytes(), password=None)
        except ValueError:
            time.sleep(0.01)
    raise RuntimeError('Instance signing key is incomplete')


def instance_public_key(storage):
    return base64.b64encode(instance_key(storage).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')


def validate_server_url(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('Invalid license server URL')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('License server requires HTTPS; HTTP is allowed only on loopback for local tests')
    return url.rstrip('/')


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A signed POST must never be redirected to another recipient or changed to GET.
        raise ValueError('License server redirects are not allowed')


def verify_response(envelope, request_payload, public_pem):
    if not isinstance(envelope, dict) or not isinstance(envelope.get('payload'), dict):
        raise ValueError('Unsigned or incomplete license response')
    response = envelope['payload']
    key = serialization.load_pem_public_key(public_pem.encode('utf-8'))
    key.verify(base64.b64decode(envelope['signature'], validate=True), canonical(response))
    for name in ('license_id','hardware_id','instance_uuid','request_nonce'):
        if response.get(name) != request_payload.get(name):
            raise ValueError('License response belongs to another request or installation')
    if response.get('request_hash') != hashlib.sha256(canonical(request_payload)).hexdigest():
        raise ValueError('License response request hash mismatch')
    now = time.time()
    if not isinstance(response.get('issued_epoch'), (int,float)) or not isinstance(response.get('expires_epoch'), (int,float)):
        raise ValueError('Missing response validity interval')
    if response['issued_epoch'] > now + 30 or response['expires_epoch'] <= now or response['expires_epoch'] - response['issued_epoch'] > 120:
        raise ValueError('Expired license response')
    if type(response.get('valid')) is not bool or response.get('status') not in (
        'active','over_quota','revoked','suspended','blacklisted','hwid_mismatch','unlicensed','expired','instance_limit'):
        raise ValueError('Invalid license response status')
    if response['valid'] != (response['status'] in ('active','over_quota')):
        raise ValueError('Inconsistent license response status')
    return response


def exchange(server_url, payload, storage, public_pem):
    url = validate_server_url(server_url) + '/api/v1/heartbeat/ping'
    request_payload = dict(payload, request_nonce=secrets.token_hex(24), request_epoch=int(time.time()))
    key = instance_key(storage)
    body = {'payload': request_payload, 'signature': base64.b64encode(key.sign(canonical(request_payload))).decode('ascii'),
            'public_key': instance_public_key(storage)}
    req = urllib.request.Request(url, data=canonical(body), headers={'Content-Type':'application/json','User-Agent':'MAX-RADIUS-License/2.0'}, method='POST')
    opener = urllib.request.build_opener(SecureRedirect())
    try:
        response = opener.open(req, timeout=6)
    except urllib.error.HTTPError as exc:
        # Unauthenticated requests are not revocations and must never modify local state.
        raise ValueError('License server rejected instance authentication: HTTP ' + str(exc.code)) from exc
    with response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('License server response is too large')
        return verify_response(json.loads(raw), request_payload, public_pem)
