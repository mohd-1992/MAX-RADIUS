#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAX RADIUS - Master License Key Generator Tool
Used exclusively by the software owner/developer to generate digitally signed,
tamper-proof Ed25519 license keys for client servers.

Usage Examples:
1. Generate lifetime license for specific Machine ID:
   python generate_license.py --client "Al-Amal Telecom" --hwid "MAX-EB2B-EE05-498A-E8EB" --subs 5000 --nas 20 --lifetime

2. Generate 1-year license for specific Machine ID:
   python generate_license.py --client "Shabaka Plus" --hwid "MAX-EB2B-EE05-498A-E8EB" --subs 2000 --nas 10 --days 365

3. Generate universal unlocked demo license (ANY hardware):
   python generate_license.py --client "Universal Demo" --hwid ANY --subs 1000 --days 30
"""

import os
import sys
import json
import base64
import random
import string
import argparse
import datetime
from pathlib import Path

# Ensure utf-8 output on all systems including Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("Error: 'cryptography' package is required. Install via: pip install cryptography")
    sys.exit(1)

KEYS_DIR = Path(__file__).resolve().parent.parent / 'storage' / 'keys'
PRIVATE_KEY_FILE = KEYS_DIR / 'master_private_key.pem'
PUBLIC_KEY_FILE = KEYS_DIR / 'master_public_key.pem'

def ensure_master_keypair():
    """Ensures Master Ed25519 keypair exists. Generates new if not found."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    
    if PRIVATE_KEY_FILE.exists() and PUBLIC_KEY_FILE.exists():
        priv_bytes = PRIVATE_KEY_FILE.read_bytes()
        private_key = serialization.load_pem_private_key(priv_bytes, password=None)
        pub_bytes = PUBLIC_KEY_FILE.read_bytes()
        public_key = serialization.load_pem_public_key(pub_bytes)
        return private_key, public_key

    print("🔑 Generating new Master Ed25519 Keypair for MAX RADIUS...")
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    PRIVATE_KEY_FILE.write_bytes(priv_pem)
    PUBLIC_KEY_FILE.write_bytes(pub_pem)
    print(f"✅ Master Private Key saved to: {PRIVATE_KEY_FILE}")
    print(f"✅ Master Public Key saved to: {PUBLIC_KEY_FILE}\n")
    
    return private_key, public_key

def generate_license(client_name, hwid, plan_tier="Enterprise", max_subs=5000, max_nas=15, max_managers=10, days=365, is_lifetime=False, out_file=None):
    private_key, public_key = ensure_master_keypair()
    
    rand_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    license_id = f"MAX-LIC-{datetime.datetime.utcnow().strftime('%Y%m%d')}-{rand_suffix}"
    
    now = datetime.datetime.utcnow()
    if is_lifetime:
        expires_at = "2099-12-31T23:59:59Z"
    else:
        exp_dt = now + datetime.timedelta(days=days)
        expires_at = exp_dt.strftime("%Y-%m-%dT23:59:59Z")
        
    payload = {
        "license_id": license_id,
        "client_name": client_name,
        "plan_tier": plan_tier,
        "hardware_id": hwid.strip().upper(),
        "is_lifetime": is_lifetime,
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at,
        "limits": {
            "max_subscribers": int(max_subs),
            "max_nas": int(max_nas),
            "max_managers": int(max_managers)
        },
        "features": {
            "user_portal": True,
            "automation_rules": True,
            "gis_map": True,
            "autoheal": True,
            "accounting_archiver": True,
            "radius_simulator": True,
            "traffic_analytics": True,
            "api_access": True,
            "white_label": True
        }
    }
    
    # Sign payload
    canonical_json = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    data_bytes = canonical_json.encode('utf-8')
    signature = private_key.sign(data_bytes)
    sig_b64 = base64.b64encode(signature).decode('utf-8')
    
    package = {
        "payload": payload,
        "signature": sig_b64
    }
    
    pkg_json_str = json.dumps(package, indent=2, ensure_ascii=False)
    pkg_base64 = base64.b64encode(json.dumps(package, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).decode('utf-8')
    
    print("=" * 70)
    print("🌟 تم إصدار وتوقيع ترخيص MAX RADIUS بنجاح 🌟")
    print("=" * 70)
    print(f"📌 رقم الترخيص (License ID):   {license_id}")
    print(f"👤 اسم العميل (Client):         {client_name}")
    print(f"🔒 بصمة الجهاز (Machine ID):    {hwid}")
    print(f"📦 الباقة (Plan Tier):          {plan_tier}")
    print(f"👥 حد المشتركين (Max Subs):     {max_subs:,}")
    print(f"📡 حد الراوترات (Max NAS):       {max_nas}")
    print(f"⏳ الصلاحية (Validity):          {'مدى الحياة (Lifetime) 🟢' if is_lifetime else f'{days} يوم (ينتهي في: {expires_at[:10]})'}")
    print("=" * 70)
    print("\n📋 كود التفعيل المباشر (Base64 Token - انسخ هذا الكود للعميل):")
    print("-" * 70)
    print(pkg_base64)
    print("-" * 70)
    
    if out_file:
        out_path = Path(out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(pkg_json_str, encoding='utf-8')
        print(f"\n💾 تم حفظ ملف الترخيص (.lic) في: {out_path.resolve()}")
        
    return pkg_base64

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MAX RADIUS Master License Key Generator")
    parser.add_argument('--client', '-c', required=True, help="Client or ISP Network Name (e.g. 'Al-Amal Telecom')")
    parser.add_argument('--hwid', '-w', required=True, help="Target Hardware Machine ID (e.g. 'MAX-EB2B-EE05-498A-E8EB' or 'ANY')")
    parser.add_argument('--tier', '-t', default="Enterprise", help="Plan Tier (Enterprise, Pro, Ultimate)")
    parser.add_argument('--subs', '-s', type=int, default=5000, help="Max active subscribers limit (Default: 5000)")
    parser.add_argument('--nas', '-n', type=int, default=20, help="Max NAS routers limit (Default: 20)")
    parser.add_argument('--managers', '-m', type=int, default=10, help="Max managers/agents limit (Default: 10)")
    parser.add_argument('--days', '-d', type=int, default=365, help="Validity duration in days (Default: 365)")
    parser.add_argument('--lifetime', '-l', action='store_true', help="Issue lifetime permanent license")
    parser.add_argument('--out', '-o', help="Output file path to save .lic file (e.g. 'client.lic')")
    
    args = parser.parse_args()
    generate_license(
        client_name=args.client,
        hwid=args.hwid,
        plan_tier=args.tier,
        max_subs=args.subs,
        max_nas=args.nas,
        max_managers=args.managers,
        days=args.days,
        is_lifetime=args.lifetime,
        out_file=args.out
    )
