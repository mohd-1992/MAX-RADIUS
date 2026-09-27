#!/usr/bin/env python3
import socket
import threading
import time

ROUTEROS_IP = "10.5.5.1"
FREERADIUS_IP = "172.21.0.4"

def pipe(src, dst):
    try:
        while True:
            data = src.recv(32768)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass

def forward_tcp(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", port))
        server.listen(100)
    except Exception:
        return

    while True:
        try:
            client_sock, addr = server.accept()
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.settimeout(5.0)
            try:
                target_sock.connect((ROUTEROS_IP, port))
                target_sock.settimeout(None)
                client_sock.settimeout(None)
            except Exception:
                client_sock.close()
                continue
            
            threading.Thread(target=pipe, args=(client_sock, target_sock), daemon=True).start()
            threading.Thread(target=pipe, args=(target_sock, client_sock), daemon=True).start()
        except Exception:
            break

def forward_udp_radius(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    while True:
        try:
            data, client_addr = sock.recvfrom(8192)
            fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fwd.settimeout(3.0)
            fwd.sendto(data, (FREERADIUS_IP, port))
            resp, _ = fwd.recvfrom(8192)
            sock.sendto(resp, client_addr)
            fwd.close()
        except Exception:
            pass

def forward_udp_coa(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except Exception:
        return
    while True:
        try:
            data, addr = sock.recvfrom(8192)
            fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fwd.sendto(data, (ROUTEROS_IP, port))
            fwd.close()
        except Exception:
            pass

if __name__ == "__main__":
    # TCP Proxies: 80 (WebFig), 8291 (Winbox), 8728 (API)
    for p in [80, 8291, 8728]:
        threading.Thread(target=forward_tcp, args=(p,), daemon=True).start()
    
    # UDP Proxies: 1812 (RADIUS Auth), 1813 (RADIUS Acct)
    for p in [1812, 1813]:
        threading.Thread(target=forward_udp_radius, args=(p,), daemon=True).start()
    
    # UDP Proxy: 3799 (CoA)
    threading.Thread(target=forward_udp_coa, args=(3799,), daemon=True).start()

    while True:
        time.sleep(3600)
