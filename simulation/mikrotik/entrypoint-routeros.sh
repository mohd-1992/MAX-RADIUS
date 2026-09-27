#!/usr/bin/env bash

QEMU_BRIDGE_ETH1='qemubr1'
default_dev1='eth0'
DUMMY_DHCPD_IP='10.0.0.1'
DHCPD_CONF_FILE='/routeros/dhcpd.conf'

/routeros/generate-dhcpd-conf.py $QEMU_BRIDGE_ETH1 > $DHCPD_CONF_FILE

function prepare_intf() {
   ip addr flush dev $1 2>/dev/null || true
   ip link add $2 type bridge 2>/dev/null || true
   ip link set dev $1 master $2 2>/dev/null || true
   ip link set dev $1 up 2>/dev/null || true
   ip link set dev $2 up 2>/dev/null || true
}

prepare_intf $default_dev1 $QEMU_BRIDGE_ETH1
ip addr add 10.0.0.1/24 dev qemubr1 2>/dev/null || true

if ip link show eth1 >/dev/null 2>&1; then
   prepare_intf eth1 qemubr2
fi

udhcpd -I $DUMMY_DHCPD_IP -f $DHCPD_CONF_FILE &

cat << 'EOF' > /routeros/qemu-ifup-1
#!/usr/bin/env bash
ip link set dev $1 up
ip link set dev $1 master qemubr1
EOF
chmod +x /routeros/qemu-ifup-1

cat << 'EOF' > /routeros/qemu-ifup-2
#!/usr/bin/env bash
ip link set dev $1 up
ip link set dev $1 master qemubr2
EOF
chmod +x /routeros/qemu-ifup-2

QEMU_NICS="-nic tap,id=qemu1,mac=54:05:AB:CD:12:31,script=/routeros/qemu-ifup-1,downscript=/routeros/qemu-ifdown"

if ip link show eth1 >/dev/null 2>&1; then
   QEMU_NICS="$QEMU_NICS -nic tap,id=qemu2,mac=54:05:AB:CD:12:32,script=/routeros/qemu-ifup-2,downscript=/routeros/qemu-ifdown"
fi

exec qemu-system-x86_64    -nographic -serial mon:stdio    -vnc 0.0.0.0:0    -m 512    -smp 4,sockets=1,cores=4,threads=1    $QEMU_NICS    "$@"    -hda $ROUTEROS_IMAGE
