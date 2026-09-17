# ==============================================================================
# REAL ROUTEROS HOTSPOT & DHCP PROVISIONING (ETHER2 REAL SIMULATION)
# ether1 -> Management & FreeRADIUS (172.21.0.100)
# ether2 -> Hotspot Gateway (10.5.5.1/24) with DHCP Server & Hotspot
# ==============================================================================

# 1. IP Address on ether2
/ip address
add address=10.5.5.1/24 network=10.5.5.0 interface=ether2 comment="Hotspot Gateway IP on ether2"

# 2. IP Pool for DHCP & Hotspot
/ip pool
add name=hs-dhcp-pool ranges=10.5.5.10-10.5.5.100 comment="Client DHCP Pool"

# 3. DHCP Server on ether2
/ip dhcp-server
add name=hs-dhcp interface=ether2 address-pool=hs-dhcp-pool lease-time=1h disabled=no

/ip dhcp-server network
add address=10.5.5.0/24 gateway=10.5.5.1 dns-server=10.5.5.1,1.1.1.1 comment="Hotspot Network"

# 4. Hotspot Server Profile with FreeRADIUS
/ip hotspot profile
add name=hsprof-ether2 hotspot-address=10.5.5.1 dns-name="hotspot.local" use-radius=yes radius-accounting=yes radius-interim-update=1m login-by=http-chap,http-pap,mac-cookie html-directory=hotspot

# 5. Hotspot Server on ether2
/ip hotspot
add name=hs-ether2 interface=ether2 profile=hsprof-ether2 address-pool=hs-dhcp-pool disabled=no

# 6. RADIUS Client pointing to FreeRADIUS container (172.21.0.4)
/radius
add address=172.21.0.4 secret="testing123" service=hotspot authentication-port=1812 accounting-port=1813 timeout=3000ms require-message-auth=no comment="MAX RADIUS Server"

# 7. Incoming CoA
/radius incoming
set accept=yes port=3799

# 8. DNS & Firewall NAT
/ip dns
set allow-remote-requests=yes
/ip dns static
add name=hotspot.local address=10.5.5.1

/ip firewall nat
add chain=srcnat action=masquerade out-interface=ether1 comment="NAT to Internet"

:log info ">>> REAL ETHER2 HOTSPOT & DHCP PROVISIONING COMPLETE <<<"
