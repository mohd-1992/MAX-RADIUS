# -*- coding: utf-8 -*-
"""
RADIUS CoA / Disconnect-Request implementation compliant with RFC 3576 / RFC 5176 & MikroTik RouterOS.
Enables immediate disconnection and rate-limit modification of active Hotspot and PPPoE sessions.
"""

import socket
import struct
import hashlib
import os
import time

CODE_DISCONNECT_REQUEST = 40
CODE_DISCONNECT_ACK     = 41
CODE_DISCONNECT_NAK     = 42
CODE_COA_REQUEST        = 43
CODE_COA_ACK            = 44
CODE_COA_NAK            = 45

ATTR_USER_NAME           = 1
ATTR_NAS_IP_ADDRESS      = 4
ATTR_FRAMED_IP_ADDRESS   = 8
ATTR_CALLING_STATION_ID  = 31
ATTR_ACCT_SESSION_ID     = 44
ATTR_ERROR_CAUSE         = 101

ERROR_CAUSE_MAP = {
    201: 'Residual Session Context Removed',
    202: 'Invalid EAP Packet (Ignored)',
    401: 'Unsupported Attribute',
    402: 'Missing Attribute',
    403: 'NAS Identification Mismatch (خطأ في المفتاح المشترك Secret أو عنوان IP)',
    404: 'Invalid Request',
    405: 'Unsupported Service',
    406: 'Unsupported Extension',
    501: 'Administratively Prohibited',
    502: 'Request Not Routable (Proxy)',
    503: 'Session Context Not Found (لا توجد جلسة نشطة بهذا الاسم حالياً)',
    504: 'Session Context Not Removable',
    505: 'Other / Unknown Session Failure',
    506: 'Resources Unavailable',
    507: 'Request Initiated'
}

ATTR_VENDOR_SPECIFIC     = 26

def encode_vsa_rate_limit(rate_limit_str):
    """Encodes MikroTik-Rate-Limit Vendor-Specific Attribute (Vendor 14988, Type 8)."""
    val_bytes = rate_limit_str.encode('utf-8')
    sub_attr = struct.pack('!BB', 8, len(val_bytes) + 2) + val_bytes
    vsa_data = struct.pack('!I', 14988) + sub_attr
    return struct.pack('!BB', ATTR_VENDOR_SPECIFIC, len(vsa_data) + 2) + vsa_data

def encode_attribute(attr_type, value):
    if isinstance(value, str):
        val_bytes = value.encode('utf-8')
    elif isinstance(value, int):
        val_bytes = struct.pack('!I', value)
    elif isinstance(value, bytes):
        val_bytes = value
    else:
        raise ValueError(f'Unsupported attribute type: {type(value)}')
    attr_len = len(val_bytes) + 2
    if attr_len > 255:
        raise ValueError(f'Attribute value too long: {attr_len} bytes')
    return struct.pack('!BB', attr_type, attr_len) + val_bytes

def encode_ip_attribute(attr_type, ip_str):
    ip_bytes = socket.inet_aton(ip_str)
    return struct.pack('!BB', attr_type, 6) + ip_bytes

class RadiusCoaClient:
    def __init__(self, nas_ip, secret, port=3799, timeout=3.0):
        self.nas_ip = nas_ip
        self.secret = secret.encode('utf-8') if isinstance(secret, str) else secret
        self.port = int(port)
        self.timeout = float(timeout)

    def disconnect_user(self, username=None, framed_ip=None, session_id=None, mac_address=None, is_test_probe=False):
        """
        Sends an RFC 5176 Disconnect-Request (Code 40) to MikroTik Router.
        """
        if not any([username, framed_ip, session_id, mac_address]):
            return {
                'success': False,
                'status': 'error',
                'code': None,
                'message': 'يجب تحديد معرّف واحد على الأقل للمشترك (اسم المستخدم، عنوان IP، أو رقم الجلسة).'
            }

        attrs = bytearray()
        if username:
            attrs.extend(encode_attribute(ATTR_USER_NAME, username))
        if framed_ip:
            try:
                attrs.extend(encode_ip_attribute(ATTR_FRAMED_IP_ADDRESS, framed_ip))
            except Exception:
                pass
        if session_id:
            attrs.extend(encode_attribute(ATTR_ACCT_SESSION_ID, session_id))
        if mac_address:
            clean_mac = mac_address.replace('-', ':').upper()
            attrs.extend(encode_attribute(ATTR_CALLING_STATION_ID, clean_mac))

        identifier = os.urandom(1)[0]
        length = 20 + len(attrs)
        
        # In RFC 5176 Disconnect-Request, the Request Authenticator is MD5(Code + ID + Length + 16-zeroes + Attrs + Secret)
        auth_zeroes = b'\x00' * 16
        auth_preimage = struct.pack('!BBH', CODE_DISCONNECT_REQUEST, identifier, length) + auth_zeroes + bytes(attrs) + self.secret
        authenticator = hashlib.md5(auth_preimage).digest()

        packet = struct.pack('!BBH', CODE_DISCONNECT_REQUEST, identifier, length) + authenticator + bytes(attrs)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)

        t_start = time.time()
        try:
            sock.sendto(packet, (self.nas_ip, self.port))
            data, addr = sock.recvfrom(4096)
            latency_ms = max(0.5, round((time.time() - t_start) * 1000, 1))
            
            if len(data) < 20:
                return {
                    'success': False,
                    'status': 'invalid_response',
                    'code': None,
                    'latency_ms': latency_ms,
                    'message': 'استجابة غير صالحة من الراوتر (الحزمة قصيرة جداً).'
                }
                
            resp_code, resp_id, resp_len = struct.unpack('!BBH', data[:4])
            resp_authenticator = data[4:20]
            resp_attrs = data[20:resp_len]

            expected_auth_preimage = struct.pack('!BBH', resp_code, resp_id, resp_len) + authenticator + resp_attrs + self.secret
            expected_auth = hashlib.md5(expected_auth_preimage).digest()
            is_valid_auth = (expected_auth == resp_authenticator)

            if resp_code == CODE_DISCONNECT_ACK:
                return {
                    'success': True,
                    'status': 'ack',
                    'code': 41,
                    'latency_ms': latency_ms,
                    'message': f'تم استلام Disconnect-ACK من الراوتر ({self.nas_ip}:{self.port}) وفصل الجلسة بنجاح خلال {latency_ms} ms.',
                    'verified': is_valid_auth
                }
            elif resp_code == CODE_DISCONNECT_NAK:
                error_desc = 'رفض الراوتر طلب الفصل (Disconnect-NAK).'
                error_code_val = None
                if len(resp_attrs) >= 6:
                    idx = 0
                    while idx < len(resp_attrs):
                        a_type = resp_attrs[idx]
                        a_len = resp_attrs[idx+1]
                        if a_type == ATTR_ERROR_CAUSE and a_len == 6:
                            error_code_val = struct.unpack('!I', resp_attrs[idx+2:idx+6])[0]
                            error_desc = f'Disconnect-NAK: {ERROR_CAUSE_MAP.get(error_code_val, f"Error {error_code_val}")}'
                            break
                        idx += a_len

                # If this is a test probe and the router replied with 406 or 503 or 403 or NAK,
                # this proves the CoA port is open and the router is listening!
                if is_test_probe:
                    if error_code_val == 403:
                        return {
                            'success': False,
                            'status': 'secret_mismatch',
                            'code': 42,
                            'error_cause': 403,
                            'latency_ms': latency_ms,
                            'message': f'استجاب الراوتر ({self.nas_ip}:{self.port}) لكن المفتاح المشترك (Secret) غير متطابق (403 NAS Mismatch). تأكد من صحة Secret.',
                            'verified': is_valid_auth
                        }
                    return {
                        'success': True,
                        'status': 'probe_success',
                        'code': 42,
                        'error_cause': error_code_val,
                        'latency_ms': latency_ms,
                        'message': f'تم الاتصال بنجاح بميزة CoA في راوتر MikroTik ({self.nas_ip}:{self.port}) خلال {latency_ms} ms! استجاب منفذ الراوتر مؤكداً صحة الربط وجاهزية استقبال أوامر الفصل الفوري (RFC 5176).',
                        'verified': is_valid_auth
                    }

                return {
                    'success': False,
                    'status': 'nak',
                    'code': 42,
                    'error_cause': error_code_val,
                    'latency_ms': latency_ms,
                    'message': error_desc,
                    'verified': is_valid_auth
                }
            else:
                return {
                    'success': False,
                    'status': 'unknown_code',
                    'code': resp_code,
                    'latency_ms': latency_ms,
                    'message': f'تم استلام كود RADIUS غير متوقع: {resp_code}'
                }

        except socket.timeout:
            return {
                'success': False,
                'status': 'timeout',
                'code': None,
                'latency_ms': None,
                'message': f'انتهت مهلة الانتظار (Timeout) - لم يستجب الراوتر {self.nas_ip}:{self.port}. تأكد من تفعيل RADIUS Incoming في MikroTik عبر: /radius incoming set accept=yes port={self.port}'
            }
        except socket.error as e:
            return {
                'success': False,
                'status': 'socket_error',
                'code': None,
                'latency_ms': None,
                'message': f'خطأ شبكة أثناء الاتصال بالراوتر ({self.nas_ip}:{self.port}): {str(e)}'
            }
        finally:
            sock.close()

    def modify_rate_limit(self, username=None, framed_ip=None, session_id=None, mac_address=None, rate_limit='10M/10M'):
        """
        Sends an RFC 5176 CoA-Request (Code 43) to MikroTik Router to change bandwidth on the fly.
        """
        if not any([username, framed_ip, session_id, mac_address]):
            return {
                'success': False,
                'status': 'error',
                'code': None,
                'message': 'يجب تحديد معرّف واحد على الأقل للمشترك.'
            }

        attrs = bytearray()
        if username:
            attrs.extend(encode_attribute(ATTR_USER_NAME, username))
        if framed_ip:
            try:
                attrs.extend(encode_ip_attribute(ATTR_FRAMED_IP_ADDRESS, framed_ip))
            except Exception:
                pass
        if session_id:
            attrs.extend(encode_attribute(ATTR_ACCT_SESSION_ID, session_id))
        if mac_address:
            clean_mac = mac_address.replace('-', ':').upper()
            attrs.extend(encode_attribute(ATTR_CALLING_STATION_ID, clean_mac))

        formatted_rate = rate_limit.strip()
        if '/' not in formatted_rate:
            formatted_rate = f"{formatted_rate}/{formatted_rate}"

        attrs.extend(encode_vsa_rate_limit(formatted_rate))

        identifier = os.urandom(1)[0]
        length = 20 + len(attrs)
        auth_zeroes = b'\x00' * 16
        auth_preimage = struct.pack('!BBH', CODE_COA_REQUEST, identifier, length) + auth_zeroes + bytes(attrs) + self.secret
        authenticator = hashlib.md5(auth_preimage).digest()

        packet = struct.pack('!BBH', CODE_COA_REQUEST, identifier, length) + authenticator + bytes(attrs)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)

        t_start = time.time()
        try:
            sock.sendto(packet, (self.nas_ip, self.port))
            data, addr = sock.recvfrom(4096)
            latency_ms = max(0.5, round((time.time() - t_start) * 1000, 1))

            if len(data) < 20:
                return {
                    'success': False,
                    'status': 'invalid_response',
                    'code': None,
                    'latency_ms': latency_ms,
                    'message': 'استجابة غير صالحة من الراوتر.'
                }

            resp_code, resp_id, resp_len = struct.unpack('!BBH', data[:4])
            if resp_code == CODE_COA_ACK:
                return {
                    'success': True,
                    'status': 'ack',
                    'code': 44,
                    'latency_ms': latency_ms,
                    'message': f'تم استلام CoA-ACK وتطبيق السرعة {formatted_rate} بنجاح خلال {latency_ms} ms.'
                }
            else:
                return {
                    'success': False,
                    'status': 'nak',
                    'code': resp_code,
                    'latency_ms': latency_ms,
                    'message': f'تم رفض طلب تغيير السرعة من الراوتر (كود: {resp_code}).'
                }
        except socket.timeout:
            return {
                'success': False,
                'status': 'timeout',
                'message': 'انتهت مهلة استجابة الراوتر (Timeout).'
            }
        except socket.error as e:
            return {
                'success': False,
                'status': 'socket_error',
                'message': f'خطأ شبكة أثناء الاتصال بالراوتر: {str(e)}'
            }
        finally:
            sock.close()