#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Registers the Mock RouterOS into MAX RADIUS database (nas and wisp_nas_devices tables).
"""
import os
import sys

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database.db import execute_write, query_one

def register_mock_nas(nas_ip="172.21.0.100", secret="testing123", name="Mock-RouterOS-Gateway"):
    print(f"[*] Registering Mock NAS [{name}] ({nas_ip}) into MAX RADIUS...")
    
    # 1. FreeRADIUS standard 'nas' table
    execute_write("DELETE FROM nas WHERE nasname = ?", (nas_ip,))
    execute_write('''
        INSERT INTO nas (nasname, shortname, type, secret, description)
        VALUES (?, ?, 'other', ?, 'Docker Mock RouterOS Gateway')
    ''', (nas_ip, name, secret))
    
    # 2. WISP Management 'wisp_nas_devices' table (if table exists)
    try:
        execute_write("DELETE FROM wisp_nas_devices WHERE ip_address = ?", (nas_ip,))
        execute_write('''
            INSERT INTO wisp_nas_devices (name, ip_address, secret, api_port, api_username, api_password, coa_port, is_active)
            VALUES (?, ?, ?, 8728, 'admin', 'admin', 3799, 1)
        ''', (name, nas_ip, secret))
    except Exception as e:
        print(f"[!] Optional wisp_nas_devices notice: {e}")
    
    print("[+] Mock NAS Router successfully registered and active in MAX RADIUS!")

if __name__ == '__main__':
    register_mock_nas()
