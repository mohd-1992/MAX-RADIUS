#!/bin/bash
set -e

echo "===================================================================="
echo "  🚀 Starting WISP FreeRADIUS & MikroTik Manager (MySQL Edition)"
echo "  📡 Web Port: 5090 | RADIUS UDP Ports: 18120 / 18130 / 37990"
echo "===================================================================="

# Ensure directories exist
mkdir -p /app/storage/backups /app/storage/uploads /app/storage/logs

# Execute Python launcher (Initializes Schema & starts Web + RADIUS daemons)
# Ensure direct container-level route for L2TP subnet to L2TP container
ip route replace 192.168.44.0/24 via 172.18.0.10 2>/dev/null || true

exec python run.py