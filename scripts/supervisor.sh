#!/bin/bash
exec >> /var/log/vpn_supervisor.log 2>&1
echo "[$(date)] Starting VPN & RADIUS supervisor..."

which socat >/dev/null 2>&1 || apk add --no-cache socat dnsmasq freeradius-utils iproute2

while ! pidof vpnserver >/dev/null 2>&1; do
  sleep 1
done

for i in $(seq 1 30); do
  if /usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD About >/dev/null 2>&1; then
    echo "SoftEther responsive with password."
    break
  fi
  if /usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:"" /CMD ServerPasswordSet maxpass123 >/dev/null 2>&1; then
    echo "SoftEther password set."
    break
  fi
  sleep 1
done

/usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD HubCreate DEFAULT /PASSWORD:maxpass123 2>/dev/null || true
/usr/vpnserver/vpncmd localhost /SERVER /CSV /HUB:DEFAULT /PASSWORD:maxpass123 /CMD SecureNatDisable 2>/dev/null || true
/usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD IPsecEnable /L2TP:yes /L2TPv3:no /IPSEC:yes /ETHERIP:no /PSK:maxradius123 /DEFAULTHUB:DEFAULT 2>/dev/null || true
/usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD BridgeCreate DEFAULT /DEVICE:vpn /TAP:yes 2>/dev/null || true

while true; do
  if ip link show tap_vpn >/dev/null 2>&1; then
    if ! ip addr show tap_vpn | grep -q "10.10.0.1/24"; then
      ip addr replace 10.10.0.1/24 dev tap_vpn
      ip link set tap_vpn up
      echo "[$(date)] tap_vpn configured with 10.10.0.1/24"
    fi
  else
    /usr/vpnserver/vpncmd localhost /SERVER /CSV /PASSWORD:maxpass123 /CMD BridgeCreate DEFAULT /DEVICE:vpn /TAP:yes 2>/dev/null || true
  fi

  if ip addr show tap_vpn 2>/dev/null | grep -q "10.10.0.1"; then
    if ! pidof dnsmasq >/dev/null 2>&1; then
      mkdir -p /etc/dnsmasq.d
      cat << 'DCONF' > /etc/dnsmasq.conf
interface=tap_vpn
bind-interfaces
listen-address=10.10.0.1
conf-dir=/etc/dnsmasq.d,*.conf
dhcp-range=10.10.0.10,10.10.0.250,255.255.255.0,24h
dhcp-option=option:router,10.10.0.1
dhcp-option=option:dns-server,10.10.0.1,8.8.8.8
dhcp-authoritative
DCONF
      /usr/sbin/dnsmasq -C /etc/dnsmasq.conf
      echo "[$(date)] dnsmasq started."
    fi

    if ! ps aux | grep "UDP4-LISTEN:1812" | grep -v grep >/dev/null 2>&1; then
      nohup socat UDP4-LISTEN:1812,bind=10.10.0.1,fork,reuseaddr UDP4:172.18.0.3:1812 >/dev/null 2>&1 &
      echo "[$(date)] socat 1812 started."
    fi

    if ! ps aux | grep "UDP4-LISTEN:1813" | grep -v grep >/dev/null 2>&1; then
      nohup socat UDP4-LISTEN:1813,bind=10.10.0.1,fork,reuseaddr UDP4:172.18.0.3:1813 >/dev/null 2>&1 &
      echo "[$(date)] socat 1813 started."
    fi
  fi

  sleep 3
done
