# ==============================================================================
# SAFE ROUTEROS HOTSPOT PROVISIONING SCRIPT (MAX RADIUS INTEGRATION)
# Note: Hotspot is strictly configured on virtual 'hotspot-bridge', NOT on ether1.
# ==============================================================================

# 1. Create Virtual Hotspot Bridge
/interface bridge
add name=hotspot-bridge comment="Virtual Isolated Hotspot Bridge"

# 2. Assign IP Address & Network to Bridge
/ip address
add address=10.5.50.1/24 network=10.5.50.0 interface=hotspot-bridge comment="Hotspot Gateway IP"

# 3. IP Pool for Hotspot Users
/ip pool
add name=hs-pool ranges=10.5.50.10-10.5.50.100 comment="Hotspot Client IP Pool"

# 4. Hotspot Server Profile with FreeRADIUS Integration
/ip hotspot profile
add name=hsprof-wisp \
    hotspot-address=10.5.50.1 \
    dns-name="hotspot.local" \
    use-radius=yes \
    radius-accounting=yes \
    radius-interim-update=1m \
    login-by=http-chap,http-pap,mac-cookie \
    html-directory=hotspot

# 5. Hotspot Server Instance
/ip hotspot
add name=hs-wisp \
    interface=hotspot-bridge \
    profile=hsprof-wisp \
    address-pool=hs-pool \
    disabled=no

# 6. FreeRADIUS Server Connection
/radius
add address=172.21.0.3 \
    secret="testing123" \
    service=hotspot \
    authentication-port=1812 \
    accounting-port=1813 \
    timeout=3000ms \
    comment="MAX RADIUS Server"

# 7. Incoming CoA / Disconnect (RFC 5176)
/radius incoming
set accept=yes port=3799

# 8. DNS Settings
/ip dns
set allow-remote-requests=yes
/ip dns static
add name=hotspot.local address=10.5.50.1

:log info "MAX RADIUS Safe Hotspot Provisioning Completed Successfully"
