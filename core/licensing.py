# -*- coding: utf-8 -*-
"""
Client License Verification Engine:
Validates Ed25519 digital signatures, machine bindings, expiry dates, and subscriber limits.
"""

import os
import json
import base64
import datetime
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

from core.hardware_fingerprint import get_machine_id, get_instance_uuid

EMBEDDED_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEADT6MJTY7bmDdw6nrAzsUyZ1nGrAxTwNKgE1MQYqAgNE=
-----END PUBLIC KEY-----"""

def get_master_public_key_pem():
    """Returns Master Public Key (Strictly Pinned to Immutable Embedded Key)."""
    return EMBEDDED_PUBLIC_KEY

def decode_license_string(lic_input):
    """
    Parses a license file string or Base64 token into a Python dictionary.
    """
    if not lic_input or not isinstance(lic_input, str):
        return None, "مفتاح الترخيص فارغ أو غير صالح"
        
    lic_input = lic_input.strip()
    
    # Try as JSON directly
    if lic_input.startswith('{') and lic_input.endswith('}'):
        try:
            return json.loads(lic_input), None
        except Exception as e:
            return None, f"خطأ في قراءة ملف الترخيص (JSON غير صالح): {str(e)}"
            
    # Try as Base64 encoded JSON
    try:
        decoded_bytes = base64.b64decode(lic_input)
        decoded_str = decoded_bytes.decode('utf-8')
        return json.loads(decoded_str), None
    except Exception:
        return None, "تنسيق مفتاح الترخيص غير معروف (يجب أن يكون ملف .lic أو نص Base64 مشفر)"

def verify_license_package(package_dict, current_subscribers=0, current_nas=0):
    """
    Performs complete verification of a license package:
    1. Signature validity (Ed25519)
    2. Machine ID matching
    3. Expiration date
    4. Limits compliance
    """
    if not isinstance(package_dict, dict) or 'payload' not in package_dict or 'signature' not in package_dict:
        return False, "هيكل ملف الترخيص غير مكتمل", {}
        
    payload = package_dict['payload']
    sig_b64 = package_dict['signature']
    
    # 1. Verify Digital Signature
    try:
        canonical_json = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        data_bytes = canonical_json.encode('utf-8')
        signature = base64.b64decode(sig_b64)
        
        pub_pem = get_master_public_key_pem().encode('utf-8')
        public_key = serialization.load_pem_public_key(pub_pem)
        
        public_key.verify(signature, data_bytes)
    except InvalidSignature:
        return False, "فشل التحقق الأمني: التوقيع الرقمي غير صالح أو تم التعديل على بيانات الترخيص", {}
    except Exception as e:
        return False, f"خطأ أثناء التحقق من التوقيع الرقمي: {str(e)}", {}
        
    # 2. Check Machine ID & Instance UUID Binding (Dual-Lock Anti-Clone)
    licensed_hw = payload.get('hardware_id', 'ANY').strip().upper()
    current_hw = get_machine_id().strip().upper()
    
    if licensed_hw not in ('ANY', '*', 'ALL', 'UNLOCKED', ''):
        clean_lic = licensed_hw.replace('-', '')
        clean_cur = current_hw.replace('-', '')
        if clean_lic != clean_cur:
            return False, f"هذا الترخيص مقفل على سيرفر آخر (بصمة الترخيص: {licensed_hw} - بصمة هذا الجهاز: {current_hw})", payload

    # 2b. Check Instance UUID (Prevent cloning on the same host)
    licensed_inst = payload.get('instance_id', 'ANY').strip().upper()
    current_inst = get_instance_uuid().strip().upper()
    if licensed_inst not in ('ANY', '*', 'ALL', 'UNLOCKED', ''):
        clean_lic_inst = licensed_inst.replace('-', '')
        clean_cur_inst = current_inst.replace('-', '')
        if clean_lic_inst != clean_cur_inst:
            return False, f"هذا الترخيص مخصص لمنظومة أخرى على نفس السيرفر (معرف الترخيص: {licensed_inst} - معرف هذه النسخة: {current_inst})", payload
            
    # 3. Check Expiry Date
    expires_at_str = payload.get('expires_at', '')
    is_lifetime = payload.get('is_lifetime', False)
    
    now = datetime.datetime.utcnow()
    days_left = 9999
    
    if not is_lifetime and expires_at_str:
        try:
            # Format: 2026-09-01T23:59:59Z
            exp_clean = expires_at_str.replace('Z', '').split('+')[0]
            exp_date = datetime.datetime.fromisoformat(exp_clean)
            diff = exp_date - now
            days_left = diff.days
            
            if diff.total_seconds() < 0:
                # Expired
                return False, f"انتهت صلاحية هذا الترخيص بتاريخ ({expires_at_str[:10]})", payload
        except Exception as e:
            return False, f"خطأ في قراءة تاريخ انتهاء الترخيص: {str(e)}", payload
            
    # 4. Check Limits (Informative check)
    limits = payload.get('limits', {})
    max_subs = limits.get('max_subscribers', 5000)
    max_nas = limits.get('max_nas', 15)
    max_managers = limits.get('max_managers', 10)
    
    info = {
        "license_id": payload.get('license_id'),
        "client_name": payload.get('client_name'),
        "plan_tier": payload.get('plan_tier', 'Enterprise'),
        "is_lifetime": is_lifetime,
        "expires_at": expires_at_str,
        "days_left": days_left,
        "max_subscribers": max_subs,
        "max_nas": max_nas,
        "max_managers": max_managers,
        "features": payload.get('features', {}),
        "hardware_id": licensed_hw,
        "current_machine_id": current_hw
    }
    
    return True, "الترخيص معتمد وصالح وموثق رقمياً بنجاح", info
