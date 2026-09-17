#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAX RADIUS - 5 Concurrent Clients Live Session & Traffic Simulator (300s)
Sends standard RADIUS RFC 2865 Auth + RFC 2866 Acct Start + Interim Heartbeats for 5 minutes.
"""

import os
import sys
import time
import random
import socket
import struct
import hashlib
import threading

RADIUS_SECRET = os.getenv("RADIUS_SECRET", "testing123").encode('utf-8')
RADIUS_SERVER_IP = os.getenv("RADIUS_SERVER", "127.0.0.1")
AUTH_PORT = int(os.getenv("AUTH_PORT", 1812))
ACCT_PORT = int(os.getenv("ACCT_PORT", 1813))
NAS_IP = os.getenv("NAS_IP", "172.21.0.100")

ACCESS_REQUEST = 1
ACCESS_ACCEPT = 2
ACCOUNTING_REQUEST = 4
ACCOUNTING_RESPONSE = 5

ACCT_STATUS_START = 1
ACCT_STATUS_STOP = 2
ACCT_STATUS_INTERIM = 3

class ConcurrentHotspotClient:
    def __init__(self, username, password, client_ip, client_mac):
        self.username = username
        self.password = password
        self.ip = client_ip
        self.mac = client_mac
        self.session_id = f"HS-MOCK-{random.randint(10000, 99999)}"
        self.total_in = 0
        self.total_out = 0
        self.session_time = 0
        self.active = False
        self.stop_event = threading.Event()

    def _auth_packet(self, req_id):
        auth_bytes = os.urandom(16)
        uname = self.username.encode('utf-8')
        attr_uname = struct.pack('!BB', 1, 2 + len(uname)) + uname
        
        pwd = self.password.encode('utf-8')
        pad_len = 16 - (len(pwd) % 16)
        padded_pwd = pwd + (b'\x00' * pad_len)
        cipher_blocks = []
        last_block = auth_bytes
        for i in range(0, len(padded_pwd), 16):
            block = padded_pwd[i:i+16]
            b_hash = hashlib.md5(RADIUS_SECRET + last_block).digest()
            c_block = bytes([b ^ h for b, h in zip(block, b_hash)])
            cipher_blocks.append(c_block)
            last_block = c_block
        attr_pwd = struct.pack('!BB', 2, 2 + len(b''.join(cipher_blocks))) + b''.join(cipher_blocks)
        
        ip_p = [int(p) for p in NAS_IP.split('.')]
        attr_nas_ip = struct.pack('!BB4B', 4, 6, *ip_p)
        
        c_ip_p = [int(p) for p in self.ip.split('.')]
        attr_framed_ip = struct.pack('!BB4B', 8, 6, *c_ip_p)
        
        mac_b = self.mac.encode('utf-8')
        attr_mac = struct.pack('!BB', 31, 2 + len(mac_b)) + mac_b
        
        attrs = attr_uname + attr_pwd + attr_nas_ip + attr_framed_ip + attr_mac
        pkt_len = 20 + len(attrs)
        header = struct.pack('!BBH', ACCESS_REQUEST, req_id, pkt_len) + auth_bytes
        return header + attrs

    def _acct_packet(self, req_id, status_type):
        zero_auth = b'\x00' * 16
        uname = self.username.encode('utf-8')
        attr_uname = struct.pack('!BB', 1, 2 + len(uname)) + uname
        
        sess = self.session_id.encode('utf-8')
        attr_sess = struct.pack('!BB', 44, 2 + len(sess)) + sess
        
        attr_status = struct.pack('!BBI', 40, 6, status_type)
        attr_time = struct.pack('!BBI', 46, 6, int(self.session_time))
        
        attr_in = struct.pack('!BBI', 42, 6, self.total_in % 4294967296)
        attr_out = struct.pack('!BBI', 43, 6, self.total_out % 4294967296)
        
        ip_p = [int(p) for p in NAS_IP.split('.')]
        attr_nas_ip = struct.pack('!BB4B', 4, 6, *ip_p)
        
        c_ip_p = [int(p) for p in self.ip.split('.')]
        attr_framed_ip = struct.pack('!BB4B', 8, 6, *c_ip_p)
        
        mac_b = self.mac.encode('utf-8')
        attr_mac = struct.pack('!BB', 31, 2 + len(mac_b)) + mac_b
        
        attrs = attr_status + attr_uname + attr_sess + attr_time + attr_in + attr_out + attr_nas_ip + attr_framed_ip + attr_mac
        pkt_len = 20 + len(attrs)
        auth_hash = hashlib.md5(struct.pack('!BBH', ACCOUNTING_REQUEST, req_id, pkt_len) + zero_auth + attrs + RADIUS_SECRET).digest()
        header = struct.pack('!BBH', ACCOUNTING_REQUEST, req_id, pkt_len) + auth_hash
        return header + attrs

    def login_and_run(self, duration_sec):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3.0)
        try:
            # 1. Access-Request
            req_id = random.randint(1, 255)
            sock.sendto(self._auth_packet(req_id), (RADIUS_SERVER_IP, AUTH_PORT))
            resp, _ = sock.recvfrom(4096)
            if resp[0] == ACCESS_ACCEPT:
                print(f"[+] [{self.username}] Authenticated Successfully (IP: {self.ip}, MAC: {self.mac})")
                self.active = True
                
                # 2. Accounting-Start
                sock.sendto(self._acct_packet(random.randint(1, 255), ACCT_STATUS_START), (RADIUS_SERVER_IP, ACCT_PORT))
                sock.recvfrom(4096)
                print(f"[+] [{self.username}] Accounting Session STARTED in MAX RADIUS radacct.")
                
                # 3. Live traffic & Interim-Update loop
                start_t = time.time()
                last_interim = time.time()
                while time.time() - start_t < duration_sec and not self.stop_event.is_set():
                    time.sleep(1)
                    self.session_time += 1
                    # Traffic generation (20KB - 200KB/s)
                    self.total_out += random.randint(20000, 200000)
                    self.total_in += random.randint(5000, 50000)
                    
                    if time.time() - last_interim >= 15:
                        last_interim = time.time()
                        sock.sendto(self._acct_packet(random.randint(1, 255), ACCT_STATUS_INTERIM), (RADIUS_SERVER_IP, ACCT_PORT))
                        sock.recvfrom(4096)
                        mb_down = self.total_out / 1048576.0
                        mb_up = self.total_in / 1048576.0
                        print(f" [~] [{self.username}] Alive ({self.session_time}s) | Down: {mb_down:.2f} MB | Up: {mb_up:.2f} MB")
                
                # 4. Accounting-Stop
                sock.sendto(self._acct_packet(random.randint(1, 255), ACCT_STATUS_STOP), (RADIUS_SERVER_IP, ACCT_PORT))
                sock.recvfrom(4096)
                print(f"[*] [{self.username}] Accounting Session STOPPED.")
            else:
                print(f"[-] [{self.username}] Access-Reject from FreeRADIUS.")
        except Exception as e:
            print(f"[!] [{self.username}] Error: {e}")
        finally:
            sock.close()

def main():
    duration = 300
    if len(sys.argv) > 1 and sys.argv[1] == '--duration' and len(sys.argv) > 2:
        duration = int(sys.argv[2])
        
    vouchers = [
        ('test1', 'test1', '10.5.50.11', '50:04:00:00:00:01'),
        ('test2', 'test2', '10.5.50.12', '50:04:00:00:00:02'),
        ('test3', 'test3', '10.5.50.13', '50:04:00:00:00:03'),
        ('test4', 'test4', '10.5.50.14', '50:04:00:00:00:04'),
        ('test5', 'test5', '10.5.50.15', '50:04:00:00:00:05'),
    ]
    
    print(f"\n=======================================================================")
    print(f"  MAX RADIUS & MIKROTIK - 5-CLIENT CONCURRENT SIMULATION")
    print(f"  Duration: {duration} Seconds (5 Minutes) | Target: {RADIUS_SERVER_IP}:{AUTH_PORT}/{ACCT_PORT}")
    print(f"=======================================================================\n")
    
    threads = []
    for u, p, ip, mac in vouchers:
        client = ConcurrentHotspotClient(u, p, ip, mac)
        t = threading.Thread(target=client.login_and_run, args=(duration,))
        threads.append(t)
        t.start()
        time.sleep(0.2)
        
    print(f"\n[+] All 5 clients started concurrently!")
    print(f"[i] Open MAX RADIUS Dashboard (http://localhost:5090) to see Live Active Sessions.")
    print(f"[i] Press Ctrl+C to stop early.\n")
    
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")

if __name__ == '__main__':
    main()
