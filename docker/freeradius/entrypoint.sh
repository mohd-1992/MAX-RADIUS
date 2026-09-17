#!/bin/bash
set -e

echo "===================================================================="
echo "  🚀 Starting Official FreeRADIUS 3.x Engine with MySQL Backend"
echo "  🗄️  Target DB: ${DB_HOST:-mariadb}:${DB_PORT:-3306}/${DB_NAME:-radius_wisp}"
echo "===================================================================="

CONF_DIR="/etc/freeradius/3.0"

# Direct static route to L2TP VPN container
ip route replace 192.168.44.0/24 via 172.18.0.5 2>/dev/null || true

# Copy custom queries.conf if mounted and ensure strict permissions
if [ -f /etc/freeradius-custom/queries.conf ]; then
    echo "[FreeRADIUS] Applying custom queries.conf with hardened permissions..."
    cp -f /etc/freeradius-custom/queries.conf ${CONF_DIR}/mods-config/sql/main/mysql/queries.conf
    chmod 640 ${CONF_DIR}/mods-config/sql/main/mysql/queries.conf
    chown freerad:freerad ${CONF_DIR}/mods-config/sql/main/mysql/queries.conf
fi

# Wait for MariaDB/MySQL
echo "[FreeRADIUS] Checking MariaDB connection..."
until mariadb -h "${DB_HOST:-mariadb}" -P "${DB_PORT:-3306}" -u "${DB_USER:-radius}" -p"${DB_PASSWORD:-radpass}" -e "SELECT 1" "${DB_NAME:-radius_wisp}" > /dev/null 2>&1; do
    echo "[FreeRADIUS] Waiting for MariaDB to be ready..."
    sleep 2
done
echo "[FreeRADIUS] MariaDB is connected and healthy."

# Ensure required tables and healthcheck credentials exist
mariadb -h "${DB_HOST:-mariadb}" -P "${DB_PORT:-3306}" -u "${DB_USER:-radius}" -p"${DB_PASSWORD:-radpass}" "${DB_NAME:-radius_wisp}" << 'EOSQL'
CREATE TABLE IF NOT EXISTS nasreload (
    nasipaddress VARCHAR(15) NOT NULL,
    reloadtime DATETIME NOT NULL,
    PRIMARY KEY (nasipaddress)
) ENGINE=InnoDB;

INSERT INTO radcheck (username, attribute, op, value) 
VALUES ('healthcheck', 'Cleartext-Password', ':=', 'healthpass') 
ON DUPLICATE KEY UPDATE value='healthpass';
EOSQL

DEFAULT_SECRET="${RADIUS_SECRET_DEFAULT:-max123}"

# 1. Write the clean SQL module configuration
cat << EOF > ${CONF_DIR}/mods-available/sql
sql {
    driver = "rlm_sql_mysql"
    dialect = "mysql"

    server = "${DB_HOST:-mariadb}"
    port = ${DB_PORT:-3306}
    login = "${DB_USER:-radius}"
    password = "${DB_PASSWORD:-radpass}"
    radius_db = "${DB_NAME:-radius_wisp}"

    # Character encoding & user mapping
    encoding = "utf8mb4"
    sql_user_name = "%{User-Name}"

    # Mapping table names & attributes
    client_table = "nas"
    group_attribute = "SQL-Group"
    authcheck_table = "radcheck"
    authreply_table = "radreply"
    groupcheck_table = "radgroupcheck"
    groupreply_table = "radgroupreply"
    usergroup_table = "radusergroup"
    acct_table1 = "radacct"
    acct_table2 = "radacct"
    postauth_table = "radpostauth"

    read_groups = yes
    read_profiles = yes
    read_clients = yes

    pool {
        start = 5
        min = 4
        max = 32
        spare = 3
        uses = 0
        retry_delay = 30
        lifetime = 0
        idle_timeout = 60
    }

    \$INCLUDE \${modconfdir}/\${.:name}/main/mysql/queries.conf
}
EOF

# Enable SQL module
ln -sf ${CONF_DIR}/mods-available/sql ${CONF_DIR}/mods-enabled/sql

# 2. Write clean clients.conf (Dynamic clients from MySQL nas table + fallback for standard routers)
cat << EOF > ${CONF_DIR}/clients.conf
# -----------------------------------------------------------------------------
# FreeRADIUS Clients Configuration (MikroTik & Localhost)
# -----------------------------------------------------------------------------

client localhost {
    ipaddr = 127.0.0.1
    secret = ${DEFAULT_SECRET}
    require_message_authenticator = no
    nas_type = other
}

client localhost_ipv6 {
    ipv6addr = ::1
    secret = ${DEFAULT_SECRET}
    require_message_authenticator = no
    nas_type = other
}

client docker_internal_net {
    ipaddr = 172.18.0.0/16
    secret = ${DEFAULT_SECRET}
    require_message_authenticator = no
    nas_type = other
}

client all_mikrotik_routers {
    ipaddr = 0.0.0.0/0
    secret = ${DEFAULT_SECRET}
    require_message_authenticator = no
    nas_type = other
}
EOF

# 3. Enable SQL & single-field Hotspot card login fallback in sites-available/default
if [ -f "${CONF_DIR}/sites-available/default" ]; then
    sed -i 's/^[[:space:]]*#[[:space:]]*sql$/	sql/g' ${CONF_DIR}/sites-available/default || true
    sed -i 's/^[[:space:]]*-sql$/	sql/g' ${CONF_DIR}/sites-available/default || true

    # Inject single-field Hotspot fallback if not already injected
    if ! grep -q "HOTSPOT_SINGLE_FIELD_CARD_FALLBACK" ${CONF_DIR}/sites-available/default; then
        sed -i '/filter_username/a 	# HOTSPOT_SINGLE_FIELD_CARD_FALLBACK\n	if (!User-Name && User-Password) {\n		update request {\n			User-Name := "%{User-Password}"\n		}\n	}\n	if (User-Name && !User-Password && !CHAP-Password) {\n		update request {\n			User-Password := "%{User-Name}"\n		}\n	}' ${CONF_DIR}/sites-available/default
    fi

    # Inject Reply-Message forwarding in Post-Auth-Type REJECT
    if ! grep -q "REPLY_MESSAGE_REJECT_FORWARDING" ${CONF_DIR}/sites-available/default; then
        sed -i '/Post-Auth-Type REJECT {/a \	# REPLY_MESSAGE_REJECT_FORWARDING\n	if (&control:Reply-Message) {\n		update reply {\n			&Reply-Message := &control:Reply-Message\n		}\n	}\n	elsif (&reply:Reply-Message) {\n		update reply {\n			&Reply-Message := &reply:Reply-Message\n		}\n	}' ${CONF_DIR}/sites-available/default
    fi
fi

chown -R freerad:freerad ${CONF_DIR} 2>/dev/null || true

echo "[FreeRADIUS] Configuration verified. Launching FreeRADIUS 3.x daemon..."
exec freeradius -f -l stdout
