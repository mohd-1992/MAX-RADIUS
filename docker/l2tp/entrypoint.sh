#!/bin/bash
set -e

echo "===================================================================="
echo "  🚀 Starting MAX RADIUS Native Linux L2TP/PPP Server Engine"
echo "  📡 Protocol: User-Based Static IP Assignment via IPCP"
echo "===================================================================="

# Ensure device nodes
[ -c /dev/ppp ] || mknod /dev/ppp c 108 0 2>/dev/null || true
[ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200 2>/dev/null || true
chmod 666 /dev/ppp /dev/net/tun 2>/dev/null || true

# Forwarding
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# Initialize secrets only if not present or empty
if [ ! -s /etc/ppp/chap-secrets ]; then
    cat << 'EOF' > /etc/ppp/chap-secrets
# Secrets for authentication using CHAP & PAP
# client\tserver\tsecret\tIP addresses
"max_vpn"\t*\t"max123"\t*
*\t*\t"max123"\t*
EOF
fi

if [ ! -s /etc/ppp/pap-secrets ]; then
    cp /etc/ppp/chap-secrets /etc/ppp/pap-secrets 2>/dev/null || true
fi
chmod 600 /etc/ppp/chap-secrets /etc/ppp/pap-secrets 2>/dev/null || true

# Write xl2tpd.conf
cat << 'EOC' > /etc/xl2tpd/xl2tpd.conf
[global]
port = 1701
auth file = /etc/ppp/chap-secrets
access control = no

[lns default]
ip range = 192.168.44.100-192.168.44.250
local ip = 192.168.44.1
require authentication = yes
require chap = yes
refuse pap = no
refuse chap = no
name = maxradius_l2tp
pppoptfile = /etc/ppp/options.xl2tpd
length bit = yes
EOC

# Write options.xl2tpd if not present
if [ ! -f /etc/ppp/options.xl2tpd ]; then
cat << 'EOC' > /etc/ppp/options.xl2tpd
auth
require-chap
require-mschap-v2
refuse-pap
refuse-eap
nodefaultroute
nobsdcomp
nodeflate
novj
novjccomp
mtu 1400
mru 1400
ms-dns 192.168.44.1
ms-dns 8.8.8.8
proxyarp
lcp-echo-interval 20
lcp-echo-failure 5
logfile /var/log/pppd.log
debug
EOC
fi

cat /etc/ppp/options.xl2tpd > /etc/ppp/options 2>/dev/null || true

# Clear old iptables rules
iptables -F || true
iptables -t nat -F || true

# Forwarding & NAT
iptables -A FORWARD -j ACCEPT
iptables -t nat -A POSTROUTING -s 192.168.44.0/24 -j MASQUERADE
iptables -t nat -A POSTROUTING -o ppp+ -j MASQUERADE

# DNAT for RADIUS ports 1812 & 1813 to max_radius_core (172.18.0.3)
iptables -t nat -A PREROUTING -p udp --dport 1812 -j DNAT --to-destination 172.18.0.3:1812
iptables -t nat -A PREROUTING -p udp --dport 1813 -j DNAT --to-destination 172.18.0.3:1813

mkdir -p /var/run/xl2tpd
rm -f /var/run/xl2tpd/l2tp-control

echo "[L2TP Server] Starting xl2tpd daemon on UDP:1701..."
exec /usr/sbin/xl2tpd -D -c /etc/xl2tpd/xl2tpd.conf
