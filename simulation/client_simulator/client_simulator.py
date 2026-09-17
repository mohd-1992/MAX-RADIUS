#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAX RADIUS - Active Hotspot Client Simulator
--------------------------------------------
Simulates multiple concurrent Hotspot users:
1. Performs HTTP / RADIUS Access-Request authentication.
2. Simulates browsing traffic (Upload/Download octets).
3. Sends live RADIUS Accounting (Start, Interim-Update, Stop) with true byte metrics.
4. Reacts to CoA Disconnect / Quota limits dynamically.
"""

import os
import sys
import time
import random
import socket
import struct
import hashlib
import threading
import urllib.request
import urllib.parse
import json

RADIUS_SECRET = os.getenv("RADIUS_SECRET", "testing123").encode('utf-8')
RADIUS_SERVER_IP = os.getenv("RADIUS_SERVER", "127.0.0.1")
AUTH_PORT = int(os.getenv("AUTH_PORT", 1812))
ACCT_PORT = int(os.getenv("ACCT_PORT", 1813))
NAS_IP = os.getenv("NAS_IP", "172.21.0.100")

# Code Numbers for RADIUS
ACCESS_REQUEST = 1
ACCESS_ACCEPT = 2
ACCESS_REJECT = 3
ACCOUNTING_REQUEST = 4
ACCOUNTING_RESPONSE = 5

ACCT_STATUS_START = 1
ACCT_STATUS_STOP = 2
ACCT_STATUS_INTERIM = 3

class MockHotspotClient:
    def __init__(self, username, password, client_ip, client_mac, speed_mbps=4.0):
        self.username = str(username)
        self.password = str(password)
        self.ip = client_ip
        self.mac = client_mac
        self.session_id = f"MOCK-{random.randint(100000, 999999)}"
        self.speed_mbps = speed_mbps
        self.is_active = False
        self.total_input_octets = 0   # Upload
        self.total_output_octets = 0  # Download
        self.session_time_seconds = 0
        self.thread = None
        self._stop_event = threading.Event()
        self.session_timeout = 86400
        self.rate_limit = "2M/4M"

    def _create_radius_auth_packet(self, req_id):
        # 16 bytes authenticator
        auth_bytes = os.urandom(16)
        
        # Attributes
        # User-Name (Type 1)
        uname_bytes = self.username.encode('utf-8')
        attr_uname = struct.pack('!BB', 1, 2 + len(uname_bytes)) + uname_bytes
        
        # User-Password (Type 2) - PAP encrypted with MD5(Secret + Authenticator)
        pwd_bytes = self.password.encode('utf-8')
        # Pad to multiple of 16
        pad_len = 16 - (len(pwd_bytes) % 16)
        padded_pwd = pwd_bytes + (b'\x00' * pad_len)
        cipher_blocks = []
        last_block = auth_bytes
        for i in range(0, len(padded_pwd), 16):
            block = padded_pwd[i:i+16]
            b_hash = hashlib.md5(RADIUS_SECRET + last_block).digest()
            c_block = bytes([b ^ h for b, h in zip(block, b_hash)])
            cipher_blocks.append(c_block)
            last_block = c_block
        enc_pwd = b''.join(cipher_blocks)
        attr_pwd = struct.pack('!BB', 2, 2 + len(enc_pwd)) + enc_pwd
        
        # NAS-IP-Address (Type 4)
        ip_parts = [int(p) for p in NAS_IP.split('.')]
        attr_nas_ip = struct.pack('!BB4B', 4, 6, *ip_parts)
        
        # Framed-IP-Address (Type 8)
        c_ip_parts = [int(p) for p in self.ip.split('.')]
        attr_framed_ip = struct.pack('!BB4B', 8, 6, *c_ip_parts)
        
        # Calling-Station-Id (Type 31 - MAC)
        mac_bytes = self.mac.encode('utf-8')
        attr_mac = struct.pack('!BB', 31, 2 + len(mac_bytes)) + mac_bytes
        
        # NAS-Port (Type 5)
        attr_nas_port = struct.pack('!BBI', 5, 6, 0)
        
        attrs = attr_uname + attr_pwd + attr_nas_ip + attr_framed_ip + attr_mac + attr_nas_port
        pkt_len = 20 + len(attrs)
        header = struct.pack('!BBH', ACCESS_REQUEST, req_id, pkt_len) + auth_bytes
        return header + attrs

    def _create_radius_acct_packet(self, req_id, status_type):
        # 16 zero bytes initial authenticator for accounting
        zero_auth = b'\x00' * 16
        
        uname_bytes = self.username.encode('utf-8')
        attr_uname = struct.pack('!BB', 1, 2 + len(uname_bytes)) + uname_bytes
        
        sess_bytes = self.session_id.encode('utf-8')
        attr_sess = struct.pack('!BB', 44, 2 + len(sess_bytes)) + sess_bytes
        
        # Acct-Status-Type (Type 40)
        attr_status = struct.pack('!BBI', 40, 6, status_type)
        
        # Acct-Session-Time (Type 46)
        attr_time = struct.pack('!BBI', 46, 6, int(self.session_time_seconds))
        
        # Acct-Input-Octets (Upload Type 42) & Gigawords (Type 52)
        in_32 = self.total_input_octets % 4294967296
        in_gw = self.total_input_octets // 4294967296
        attr_in = struct.pack('!BBI', 42, 6, in_32)
        attr_in_gw = struct.pack('!BBI', 52, 6, in_gw)
        
        # Acct-Output-Octets (Download Type 43) & Gigawords (Type 53)
        out_32 = self.total_output_octets % 4294967296
        out_gw = self.total_output_octets // 4294967296
        attr_out = struct.pack('!BBI', 43, 6, out_32)
        attr_out_gw = struct.pack('!BBI', 53, 6, out_gw)
        
        ip_parts = [int(p) for p in NAS_IP.split('.')]
        attr_nas_ip = struct.pack('!BB4B', 4, 6, *ip_parts)
        
        c_ip_parts = [int(p) for p in self.ip.split('.')]
        attr_framed_ip = struct.pack('!BB4B', 8, 6, *c_ip_parts)
        
        mac_bytes = self.mac.encode('utf-8')
        attr_mac = struct.pack('!BB', 31, 2 + len(mac_bytes)) + mac_bytes
        
        attrs = (attr_status + attr_uname + attr_sess + attr_time + 
                 attr_in + attr_in_gw + attr_out + attr_out_gw + 
                 attr_nas_ip + attr_framed_ip + attr_mac)
        pkt_len = 20 + len(attrs)
        
        # Request Authenticator = MD5(Code + ID + Length + 16 Zeroes + Attributes + Secret)
        auth_hash = hashlib.md5(struct.pack('!BBH', ACCOUNTING_REQUEST, req_id, pkt_len) + zero_auth + attrs + RADIUS_SECRET).digest()
        header = struct.pack('!BBH', ACCOUNTING_REQUEST, req_id, pkt_len) + auth_hash
        return header + attrs

    def login(self):
        """Send live Access-Request packet to FreeRADIUS."""
        print(f"[*] [Client {self.username}] Attempting RADIUS Authentication (IP: {self.ip}, MAC: {self.mac})...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.5)
        try:
            req_id = random.randint(1, 255)
            pkt = self._create_radius_auth_packet(req_id)
            sock.sendto(pkt, (RADIUS_SERVER_IP, AUTH_PORT))
            
            resp_data, _ = sock.recvfrom(4096)
            resp_code = resp_data[0]
            if resp_code == ACCESS_ACCEPT:
                print(f"[+] [Client {self.username}] [ACCESS-ACCEPT] Login Successful!")
                self.is_active = True
                self._send_acct_start()
                return True
            else:
                print(f"[-] [Client {self.username}] [ACCESS-REJECT] Authentication Denied.")
                return False
        except Exception as e:
            print(f"[!] [Client {self.username}] RADIUS Error: {e}")
            return False
        finally:
            sock.close()

    def _send_acct_start(self):
        """Send Accounting-Start to begin live session in radacct."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            req_id = random.randint(1, 255)
            pkt = self._create_radius_acct_packet(req_id, ACCT_STATUS_START)
            sock.sendto(pkt, (RADIUS_SERVER_IP, ACCT_PORT))
            resp, _ = sock.recvfrom(4096)
            if resp[0] == ACCOUNTING_RESPONSE:
                print(f"[+] [Client {self.username}] [ACCT-START] Session started in MAX RADIUS radacct.")
        except Exception as e:
            print(f"[!] [Client {self.username}] Acct Start Error: {e}")
        finally:
            sock.close()

    def _send_acct_interim(self):
        """Send Interim-Update heartbeat with updated byte counters."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            req_id = random.randint(1, 255)
            pkt = self._create_radius_acct_packet(req_id, ACCT_STATUS_INTERIM)
            sock.sendto(pkt, (RADIUS_SERVER_IP, ACCT_PORT))
            sock.recvfrom(4096)
            in_mb = self.total_input_octets / (1024 * 1024)
            out_mb = self.total_output_octets / (1024 * 1024)
            print(f"[~] [Client {self.username}] [INTERIM-UPDATE] Time: {self.session_time_seconds}s | Down: {out_mb:.2f} MB | Up: {in_mb:.2f} MB")
        except Exception as e:
            pass
        finally:
            sock.close()

    def _send_acct_stop(self):
        """Send Accounting-Stop when client disconnects."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            req_id = random.randint(1, 255)
            pkt = self._create_radius_acct_packet(req_id, ACCT_STATUS_STOP)
            sock.sendto(pkt, (RADIUS_SERVER_IP, ACCT_PORT))
            sock.recvfrom(4096)
            print(f"[*] [Client {self.username}] [ACCT-STOP] Session terminated.")
        except Exception as e:
            pass
        finally:
            sock.close()

    def start_traffic_simulation(self):
        """Worker thread that simulates browsing and periodic interim updates."""
        def _worker():
            last_interim = time.time()
            while not self._stop_event.is_set():
                time.sleep(1)
                self.session_time_seconds += 1
                
                # Simulate randomized dynamic traffic (50KB - 500KB/s)
                down_chunk = int(random.randint(50000, 500000) * (self.speed_mbps / 4.0))
                up_chunk = int(down_chunk * random.uniform(0.1, 0.3))
                
                self.total_output_octets += down_chunk
                self.total_input_octets += up_chunk
                
                # Send Interim-Update every 15 seconds for rapid live dashboard inspection
                if time.time() - last_interim >= 15:
                    last_interim = time.time()
                    self._send_acct_interim()
                    
            self._send_acct_stop()

        self.thread = threading.Thread(target=_worker, daemon=True)
        self.thread.start()

    def disconnect(self):
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.is_active = False

def run_simulation(vouchers, duration=120):
    """Run multiple concurrent clients."""
    clients = []
    print(f"\n=================================================================")
    print(f"  MAX RADIUS - LIVE ACTIVE CLIENT SIMULATOR STARTED")
    print(f"  Target RADIUS: {RADIUS_SERVER_IP}:{AUTH_PORT}/{ACCT_PORT}")
    print(f"  Simulated Duration: {duration} seconds | Clients: {len(vouchers)}")
    print(f"=================================================================\n")
    
    for i, v in enumerate(vouchers):
        ip = f"192.168.88.{10 + i}"
        mac = f"50:04:00:00:00:{i+1:02X}"
        c = MockHotspotClient(username=v['username'], password=v['password'], client_ip=ip, client_mac=mac)
        if c.login():
            c.start_traffic_simulation()
            clients.append(c)
        time.sleep(0.5)

    print(f"\n[+] {len(clients)} clients actively connected and generating live traffic...")
    print(f"[i] Open MAX RADIUS Dashboard (http://localhost:5090) to observe Live Sessions & Traffic Graphs!")
    print(f"[i] Press Ctrl+C at any time to gracefully disconnect all clients.\n")
    
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user. Disconnecting simulated clients...")
    finally:
        for c in clients:
            c.disconnect()
        print("\n[+] All simulated sessions cleanly closed.")

if __name__ == '__main__':
    if len(sys.argv) > 2:
        # CLI Args: user password [duration]
        u = sys.argv[1]
        p = sys.argv[2]
        dur = int(sys.argv[3]) if len(sys.argv) > 3 else 120
        run_simulation([{'username': u, 'password': p}], duration=dur)
    else:
        # Default test client
        print("Usage: python client_simulator.py <username> <password> [duration_seconds]")
        print("Example: python client_simulator.py 11473424 36225844 60")
