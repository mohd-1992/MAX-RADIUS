#!/bin/bash
set -e

echo "===================================================================="
echo "  🚀 Starting WISP FreeRADIUS & MikroTik Manager (MySQL Edition)"
echo "  📡 Web Port: 5090 | RADIUS UDP Ports: 18120 / 18130 / 37990"
echo "===================================================================="

# Ensure directories exist
mkdir -p /app/storage/backups /app/storage/uploads /app/storage/logs

# Execute Python launcher (Initializes Schema & starts Web + RADIUS daemons)
exec python run.py