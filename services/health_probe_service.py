"""Authenticated read-only RADIUS liveness probes."""
import hashlib
import hmac
import os
import secrets
import socket
import struct

def radius_probe(port):
    host = os.environ.get('RADIUS_HOST') or os.environ.get('RADIUS_SERVER_IP') or 'radius_core'
    secret = os.environ.get('RADIUS_HEALTH_SECRET') or os.environ.get('RADIUS_SECRET_DEFAULT')
    if not secret:
        return None, 'لم يضبط سر فحص RADIUS؛ الاستجابة غير متحققة'
    try:
        ident = secrets.randbelow(256)
        auth = secrets.token_bytes(16)
        packet = struct.pack('!BBH', 12, ident, 38) + auth + b'\x50\x12' + bytes(16)
        packet = packet[:-16] + hmac.new(secret.encode(), packet, hashlib.md5).digest()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.connect((host, int(port)))
            sock.send(packet)
            reply = sock.recv(4096)
        if len(reply) < 20:
            raise ValueError('رد غير مكتمل')
        code, rid, size = struct.unpack('!BBH', reply[:4])
        if rid != ident or size != len(reply) or code not in (2, 5):
            raise ValueError('رد غير مطابق')
        expected = hashlib.md5(reply[:4] + auth + reply[20:] + secret.encode()).digest()
        if not hmac.compare_digest(expected, reply[4:20]):
            raise ValueError('توقيع رد غير صحيح')
        return True, 'وصل رد Status-Server موثق؛ هذا فحص استجابة وليس اختبار حساب مشترك'
    except Exception as exc:
        return False, 'لم تصل استجابة RADIUS موثقة: ' + str(exc)
