#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import socket
import subprocess
import threading
import logging
import time

SOCK_PATH = "/opt/max-radius/storage/wg_bridge.sock"
WG_IFACE = "wg0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True, r.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    except Exception as e:
        return False, str(e)

def gen_keypair():
    ok1, priv = run_cmd(["wg", "genkey"])
    if not ok1:
        return None, None
    try:
        p = subprocess.run(["wg", "pubkey"], input=priv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        pub = p.stdout.strip()
        return priv, pub
    except Exception:
        return None, None

def get_wg_dump():
    ok, out = run_cmd(["wg", "show", WG_IFACE, "dump"])
    if not ok:
        return []
    peers = []
    lines = out.strip().splitlines()
    for line in lines[1:]:
        parts = line.strip().split("\t")
        if len(parts) >= 8:
            peers.append({
                "public_key": parts[0],
                "preshared_key": parts[1],
                "endpoint": parts[2],
                "allowed_ips": parts[3],
                "latest_handshake": int(parts[4]) if parts[4].isdigit() else 0,
                "transfer_rx": int(parts[5]) if parts[5].isdigit() else 0,
                "transfer_tx": int(parts[6]) if parts[6].isdigit() else 0,
                "persistent_keepalive": parts[7]
            })
    return peers

def add_peer(pubkey, allowed_ip, preshared_key=None):
    cmd = ["wg", "set", WG_IFACE, "peer", pubkey, "allowed-ips", f"{allowed_ip}/32"]
    if preshared_key:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tf:
            tf.write(preshared_key)
            tf_path = tf.name
        cmd.extend(["preshared-key", tf_path])
        ok, res = run_cmd(cmd)
        try: os.remove(tf_path)
        except Exception: pass
    else:
        ok, res = run_cmd(cmd)
    
    run_cmd(["bash", "-c", f"wg-quick save {WG_IFACE} 2>/dev/null || true"])
    return ok, res

def remove_peer(pubkey):
    ok, res = run_cmd(["wg", "set", WG_IFACE, "peer", pubkey, "remove"])
    run_cmd(["bash", "-c", f"wg-quick save {WG_IFACE} 2>/dev/null || true"])
    return ok, res

def sync_peers_from_db():
    """Syncs all active WireGuard peers from MariaDB container into Linux kernel wg0."""
    try:
        cmd = [
            "docker", "exec", "max_radius_db",
            "mariadb", "-u", "root", "-prootpass", "radius_wisp", "-N", "-e",
            "SELECT public_key, tunnel_ip, IFNULL(preshared_key, '') FROM wisp_wireguard_tunnels WHERE public_key IS NOT NULL AND tunnel_ip IS NOT NULL;"
        ]
        ok, out = run_cmd(cmd)
        if ok and out:
            count = 0
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    pub = parts[0].strip()
                    ip = parts[1].strip()
                    psk = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
                    if pub and ip:
                        add_peer(pub, ip, psk)
                        count += 1
            logging.info(f"Successfully synced {count} WireGuard peers from DB to kernel {WG_IFACE}.")
    except Exception as e:
        logging.warning(f"Could not sync peers from DB: {e}")

def handle_client(conn):
    try:
        data = conn.recv(65536)
        if not data:
            return
        req = json.loads(data.decode("utf-8"))
        action = req.get("action")
        response = {"success": False}

        if action == "dump":
            response = {"success": True, "peers": get_wg_dump()}
        elif action == "genkey":
            priv, pub = gen_keypair()
            if priv and pub:
                response = {"success": True, "private_key": priv, "public_key": pub}
            else:
                response = {"success": False, "error": "Failed to generate keys"}
        elif action == "add_peer":
            pub = req.get("public_key")
            ip = req.get("allowed_ip")
            psk = req.get("preshared_key")
            ok, msg = add_peer(pub, ip, psk)
            response = {"success": ok, "message": msg}
        elif action == "remove_peer":
            pub = req.get("public_key")
            ok, msg = remove_peer(pub)
            response = {"success": ok, "message": msg}
        elif action == "sync_all":
            sync_peers_from_db()
            response = {"success": True, "peers": get_wg_dump()}
        elif action == "status":
            ok, out = run_cmd(["wg", "show", WG_IFACE])
            response = {"success": ok, "output": out}
        else:
            response = {"success": False, "error": f"Unknown action: {action}"}

        conn.sendall(json.dumps(response).encode("utf-8"))
    except Exception as e:
        conn.sendall(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
    finally:
        conn.close()

def main():
    os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
    if os.path.exists(SOCK_PATH):
        try:
            os.remove(SOCK_PATH)
        except Exception:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    server.listen(32)
    logging.info(f"WireGuard Bridge Daemon listening on {SOCK_PATH}")

    # Initial sync from DB in a background thread once DB is ready
    def delayed_sync():
        time.sleep(3)
        sync_peers_from_db()
    threading.Thread(target=delayed_sync, daemon=True).start()

    while True:
        try:
            conn, _ = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
            t.start()
        except Exception as e:
            logging.error(f"Error accepting client: {e}")

if __name__ == "__main__":
    main()
