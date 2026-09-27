#!/bin/sh
set -e

echo '[browser-init] Configuring network for MikroTik Hotspot...'

# 1. Ensure /etc/hosts has hotspot.local
if ! grep -q "hotspot.local" /etc/hosts; then
    echo "10.5.5.1 hotspot.local" >> /etc/hosts
fi

# 2. Get docker internal IP on eth0
DOCKER_IP=$(ip -4 addr show eth0 | grep inet | grep -v '10.5.5.' | awk '{print $2}' | head -n 1)

# 3. Create udhcpc script
cat << 'EOF' > /tmp/udhcpc_script.sh
#!/bin/sh
case "$1" in
    deconfig)
        ;;
    bound|renew)
        echo "[udhcpc] Obtained IP: $ip, router: $router, dns: $dns"
        # Flush any previous 10.5.5.x IPs to prevent duplicates
        for old_ip in $(ip -4 addr show dev "$interface" | grep "inet 10.5.5." | awk '{print $2}'); do
            ip addr del "$old_ip" dev "$interface" 2>/dev/null || true
        done
        
        # Add the fresh DHCP IP
        ip addr add "$ip/24" dev "$interface" 2>/dev/null || true
        
        # Keep docker bridge subnet routed directly
        ip route add 172.22.0.0/16 dev "$interface" 2>/dev/null || true
        
        # Set default gateway to MikroTik
        if [ -n "$router" ]; then
            ip route replace default via "$router" dev "$interface"
        fi
        
        # Set DNS
        > /etc/resolv.conf
        for d in ${dns:-10.5.5.1} 1.1.1.1; do
            echo "nameserver $d" >> /etc/resolv.conf
        done
        ;;
esac
EOF
chmod +x /tmp/udhcpc_script.sh

# 4. Request DHCP lease from MikroTik
if command -v udhcpc >/dev/null 2>&1; then
    echo '[browser-init] Running udhcpc with custom handler...'
    udhcpc -i eth0 -n -q -s /tmp/udhcpc_script.sh || true
else
    echo '[browser-init] Applying static Hotspot IP 10.5.5.79/24...'
    ip addr add 10.5.5.79/24 dev eth0 2>/dev/null || true
    ip route add 172.22.0.0/16 dev eth0 2>/dev/null || true
    ip route replace default via 10.5.5.1 dev eth0
    > /etc/resolv.conf
    echo "nameserver 10.5.5.1" >> /etc/resolv.conf
    echo "nameserver 1.1.1.1" >> /etc/resolv.conf
fi

echo '[browser-init] Final network configuration:'
ip -4 addr show eth0
ip route show
cat /etc/resolv.conf
