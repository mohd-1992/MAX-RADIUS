# =============================================================================
# MAX RADIUS - Mock RouterOS Auto-Configuration Script (.rsc)
# Target: MikroTik RouterOS v6.x / v7.x (Mock Container & CHR)
# =============================================================================

# 1. Router Identity
/system identity set name="MAX-RADIUS-MOCK-ROUTER"

# 2. IP Pool & Addressing for Hotspot
/ip pool add name="hs-pool-1" ranges=192.168.88.10-192.168.88.250

/ip address add address=192.168.88.1/24 interface=ether1 comment="Hotspot Gateway IP"

# 3. Hotspot Server Profile Configuration (RADIUS Authentication & Accounting)
/ip hotspot profile
add name="hsprof-maxradius" \
    hotspot-address=192.168.88.1 \
    dns-name="wifi.maxradius.net" \
    html-directory="hotspot" \
    login-by=http-chap,http-pap,mac-cookie,cookie \
    use-radius=yes \
    radius-accounting=yes \
    radius-interim-update=1m \
    radius-default-domain="" \
    radius-mac-format="XX:XX:XX:XX:XX:XX" \
    radius-location-id="MOCK_HOTSPOT_TOWER_1" \
    radius-location-name="Main-Campus-Mock"

# 4. Hotspot Server Instance
/ip hotspot
add name="hotspot1" \
    interface=ether1 \
    address-pool=hs-pool-1 \
    profile=hsprof-maxradius \
    addresses-per-mac=1 \
    idle-timeout=5m \
    keepalive-timeout=2m \
    disabled=no

# 5. Hotspot User Profile (Default Fallback)
/ip hotspot user profile
set [ find default=yes ] \
    idle-timeout=5m \
    keepalive-timeout=2m \
    status-autorefresh=1m \
    transparent-proxy=no \
    shared-users=1

# 6. RADIUS Client Configuration (Connected to max_radius_core)
/radius
add service=hotspot,login \
    address=172.21.0.3 \
    secret="testing123" \
    authentication-port=1812 \
    accounting-port=1813 \
    timeout=3000ms \
    comment="MAX-RADIUS-Core-Server"

# 7. Enable RADIUS Incoming (CoA / PoD Disconnect Requests on RFC 5176 Port 3799)
/radius incoming
set accept=yes \
    port=3799

# 8. IP Service Configuration (API, Winbox, Web)
/ip service
set api port=8728 disabled=no
set winbox port=8291 disabled=no
set www port=80 disabled=no

:log info "MAX RADIUS Mock RouterOS Hotspot and RADIUS Configuration Applied Successfully!"
