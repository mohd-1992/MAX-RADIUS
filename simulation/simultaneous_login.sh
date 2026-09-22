#!/bin/bash
# ==============================================================================
# MAX RADIUS - Concurrent 5-Voucher Hotspot & RADIUS Login Simulator
# ==============================================================================
echo "======================================================================="
echo "  🚀 Starting Simultaneous 5-Client Hotspot Login Simulation..."
echo "  ⏱️  Session Hold Duration: 300 Seconds (5 Minutes)"
echo "  🎯 Target MikroTik & MAX RADIUS System"
echo "======================================================================="

ROUTER_IP="127.0.0.1:8088"
HOLD_SECONDS=300

VOUCHERS=("test1:test1:10.5.50.11:50:04:00:00:00:01"
          "test2:test2:10.5.50.12:50:04:00:00:00:02"
          "test3:test3:10.5.50.13:50:04:00:00:00:03"
          "test4:test4:10.5.50.14:50:04:00:00:00:04"
          "test5:test5:10.5.50.15:50:04:00:00:00:05")

# Login function for each client
client_login() {
    local USER=$1
    local PASS=$2
    local IP=$3
    local MAC=$4
    
    echo "[*] [Client $USER] Connecting with IP $IP (MAC: $MAC)..."
    
    # 1. Send HTTP POST to Hotspot Login page
    curl -s -X POST "http://${ROUTER_IP}/login" \
         -H "X-Forwarded-For: ${IP}" \
         -d "username=${USER}&password=${PASS}&dst=http%3A%2F%2Fwww.google.com" \
         -o /dev/null
         
    echo "[+] [Client $USER] Logged In! Keeping session active for ${HOLD_SECONDS}s..."
}

# Launch all 5 clients concurrently in background
for item in "${VOUCHERS[@]}"; do
    IFS=":" read -r user pass ip mac <<< "$item"
    client_login "$user" "$pass" "$ip" "$mac" &
done

echo ""
echo "-----------------------------------------------------------------------"
echo "[+] All 5 clients (test1 to test5) sent concurrent login requests!"
echo "[i] Check Winbox: IP -> Hotspot -> (Active & Hosts tabs)"
echo "[i] Check MAX RADIUS Dashboard: http://localhost:5090 (Live Sessions)"
echo "-----------------------------------------------------------------------"
echo "Holding sessions active for 5 minutes (300 seconds)..."

# Run the live radius traffic simulator in parallel to maintain active accounting in radacct
python simulation/simultaneous_login.py --duration ${HOLD_SECONDS}

echo ""
echo "[✓] 5-minute simulation completed successfully."
