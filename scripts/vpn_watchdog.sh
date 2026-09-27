#!/bin/bash
# ==============================================================================
# MAX RADIUS VPS Host L2TP & VPN Watchdog Daemon (Optimized High-Efficiency Edition)
# Dynamically synchronizes L2TP Gateway IP, DHCP Pool, IPsec, and RADIUS Forwarders.
# Feature: Singleton PID Lock, Batched DB Queries, Change Detection, 30s Interval (0.01% CPU)
# ==============================================================================

PIDFILE="/tmp/max_vpn_watchdog.pid"

# 1. Singleton Lock Enforcement (Prevents multiple duplicate instances)
if [ -f "$PIDFILE" ]; then
  OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[vpn_watchdog] Daemon already running (PID $OLD_PID). Exiting."
    exit 0
  fi
fi
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE; exit 0" INT TERM EXIT

LAST_SETTINGS_HASH=""
PACKAGES_INITIALIZED=0

echo "[vpn_watchdog] Starting optimized VPN watchdog daemon (PID $$)..."

while true; do
  # Check if max_radius_l2tp container is running
  if docker ps --format '{{.Names}}' | grep -q '^max_radius_l2tp$'; then
    
    # Check packages only once or when container restarts
    if [ "$PACKAGES_INITIALIZED" -eq 0 ]; then
      if ! docker exec max_radius_l2tp which socat >/dev/null 2>&1 || ! docker exec max_radius_l2tp which dnsmasq >/dev/null 2>&1; then
        docker exec max_radius_l2tp apk add --no-cache socat dnsmasq iproute2 freeradius-utils iptables >/dev/null 2>&1
      fi
      PACKAGES_INITIALIZED=1
    fi

    # Check SoftEther tap_vpn device bridge
    if ! docker exec max_radius_l2tp ip link show tap_vpn >/dev/null 2>&1; then
      docker exec max_radius_l2tp /usr/bin/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD BridgeCreate DEFAULT /DEVICE:vpn /TAP:yes >/dev/null 2>&1 || true
    fi

    # Batch all dynamic database queries into a single call
    RAW_DATA=$(docker exec max_radius_db mysql -u root -prootpass radius_wisp -sN -e '
      SELECT `key`, value FROM wisp_system_settings WHERE `key` IN ("l2tp_gateway_ip", "l2tp_pool_start", "l2tp_pool_end");
      SELECT "TUNNEL_ENTRY", username, tunnel_ip FROM wisp_l2tp_tunnels WHERE is_enabled = 1;
    ' 2>/dev/null)

    CURRENT_HASH=$(echo "$RAW_DATA" | md5sum | awk '{print $1}')

    # Extract settings from batched output
    GW_IP=$(echo "$RAW_DATA" | awk '$1=="l2tp_gateway_ip" {print $2}')
    GW_IP=${GW_IP:-10.10.0.1}

    POOL_START=$(echo "$RAW_DATA" | awk '$1=="l2tp_pool_start" {print $2}')
    POOL_START=${POOL_START:-10.10.0.10}

    POOL_END=$(echo "$RAW_DATA" | awk '$1=="l2tp_pool_end" {print $2}')
    POOL_END=${POOL_END:-10.10.0.250}

    # If configuration changed or first run, apply network setup
    if [ "$CURRENT_HASH" != "$LAST_SETTINGS_HASH" ] || ! docker exec max_radius_l2tp ip addr show tap_vpn 2>/dev/null | grep -q "${GW_IP}/24"; then
      
      # 1. Configure IP on tap_vpn
      if ! docker exec max_radius_l2tp ip addr show tap_vpn 2>/dev/null | grep -q "${GW_IP}/24"; then
        docker exec max_radius_l2tp ip addr flush dev tap_vpn 2>/dev/null || true
        docker exec max_radius_l2tp ip addr add ${GW_IP}/24 dev tap_vpn
        docker exec max_radius_l2tp ip link set tap_vpn up
        docker exec max_radius_l2tp pkill -9 dnsmasq 2>/dev/null || true
        docker exec max_radius_l2tp pkill -9 socat 2>/dev/null || true
      fi

      # 2. Configure MASQUERADE NAT
      if ! docker exec max_radius_l2tp iptables -t nat -C POSTROUTING -o tap_vpn -j MASQUERADE 2>/dev/null; then
        docker exec max_radius_l2tp iptables -t nat -A POSTROUTING -o tap_vpn -j MASQUERADE 2>/dev/null || true
      fi

      # 3. Configure dnsmasq DHCP
      TUNNEL_LINES=$(echo "$RAW_DATA" | awk '$1=="TUNNEL_ENTRY" {print "dhcp-host=" $2 "," $3 "\ndhcp-host=" tolower($2) "," $3}')
      
      docker exec max_radius_l2tp sh -c "
mkdir -p /etc/dnsmasq.d
cat << 'EOF' > /etc/dnsmasq.conf
interface=tap_vpn
bind-interfaces
listen-address=${GW_IP}
conf-dir=/etc/dnsmasq.d,*.conf
dhcp-range=${POOL_START},${POOL_END},255.255.255.0,24h
dhcp-option=option:router,${GW_IP}
dhcp-option=option:dns-server,${GW_IP},8.8.8.8
dhcp-authoritative
EOF

cat << 'EOF' > /etc/dnsmasq.d/tunnels.conf
${TUNNEL_LINES}
EOF

pkill -9 dnsmasq 2>/dev/null || true
dnsmasq -C /etc/dnsmasq.conf
"
      LAST_SETTINGS_HASH="$CURRENT_HASH"
    fi

    # Ensure socat 1812 (RADIUS Auth Proxy)
    if ! docker exec max_radius_l2tp ps aux | grep "UDP4-LISTEN:1812,bind=${GW_IP}" | grep -v grep >/dev/null 2>&1; then
      docker exec max_radius_l2tp pkill -f "UDP4-LISTEN:1812" 2>/dev/null || true
      docker exec -d max_radius_l2tp socat UDP4-LISTEN:1812,bind=${GW_IP},fork,reuseaddr UDP4:172.18.0.3:1812
    fi

    # Ensure socat 1813 (RADIUS Acct Proxy)
    if ! docker exec max_radius_l2tp ps aux | grep "UDP4-LISTEN:1813,bind=${GW_IP}" | grep -v grep >/dev/null 2>&1; then
      docker exec max_radius_l2tp pkill -f "UDP4-LISTEN:1813" 2>/dev/null || true
      docker exec -d max_radius_l2tp socat UDP4-LISTEN:1813,bind=${GW_IP},fork,reuseaddr UDP4:172.18.0.3:1813
    fi

  else
    PACKAGES_INITIALIZED=0
  fi

  sleep 30
done
