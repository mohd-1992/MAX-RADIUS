#!/usr/bin/env bash
# MAX RADIUS: install prebuilt protected images; never clone application sources.
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run as root: sudo bash install.sh' >&2; exit 1; }
INSTALL_DIR="${MAX_INSTALL_DIR:-/opt/max-radius}"
MANIFEST_URL="${MAX_RELEASE_MANIFEST_URL:-https://raw.githubusercontent.com/mohd-1992/MAX-RADIUS/main/release.json}"
[[ "$INSTALL_DIR" == /* && "$INSTALL_DIR" != / ]] || { echo 'Invalid installation directory' >&2; exit 1; }
[[ -f /etc/os-release ]] || { echo 'Cannot detect operating system' >&2; exit 1; }
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo 'Supported: Ubuntu / Debian' >&2; exit 1;; esac
case "$(uname -m)" in x86_64|amd64) ;; *) echo 'This release requires Linux amd64' >&2; exit 1;; esac
export DEBIAN_FRONTEND=noninteractive
# Establish real host time before TLS downloads or creating license time records.
# Never step a live installation's clock automatically: refuse and leave it intact.
ensure_host_time() {
  command -v timedatectl >/dev/null || { echo 'A synchronized host clock is required (timedatectl unavailable).' >&2; return 1; }
  [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) != yes ]] || return 0
  if command -v docker >/dev/null && [[ $(docker inspect --format '{{.State.Running}}' max_radius_web 2>/dev/null || true) == true ]]; then
    echo 'Host clock is not synchronized. Existing services were left unchanged; synchronize the host before retrying.' >&2
    return 1
  fi
  timedatectl set-ntp true || { echo 'Enable a host NTP service before installing MAX RADIUS.' >&2; return 1; }
  for ((attempt=0; attempt<30; attempt++)); do
    [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) != yes ]] || return 0
    sleep 2
  done
  echo 'Host NTP synchronization was not confirmed within 60 seconds; installation stopped without starting services.' >&2
  return 1
}
ensure_host_time
nginx_default_existed=0
[[ ! -f /etc/nginx/sites-available/default ]] || nginx_default_existed=1

echo '[1/6] Preparing dependencies (waiting for package manager locks if needed)...'
apt-get -o DPkg::Lock::Timeout=600 update -q
apt-get -o DPkg::Lock::Timeout=600 install -y -q curl jq ca-certificates nginx iproute2
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version >/dev/null || { echo 'Docker Compose v2 plugin is required' >&2; exit 1; }

echo '[2/6] Downloading verified runtime-only release...'
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$MANIFEST_URL" -o "$stage/release.json"
jq -e '.format == 1 and .platform == "linux/amd64" and (.release | test("^[A-Za-z0-9._-]+$")) and (.bundle.sha256 | test("^[a-f0-9]{64}$")) and (.bundle.url | startswith("https://github.com/mohd-1992/MAX-RADIUS/releases/download/")) and ([.images.web,.images.core,.images.l2tp] | all(test("^mohd777/max-radius-(web|core|l2tp)@sha256:[a-f0-9]{64}$")))' "$stage/release.json" >/dev/null
release=$(jq -r .release "$stage/release.json")
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$(jq -r .bundle.url "$stage/release.json")" -o "$stage/runtime.tar.gz"
printf '%s  %s\n' "$(jq -r .bundle.sha256 "$stage/release.json")" "$stage/runtime.tar.gz" | sha256sum -c - >/dev/null
# Reject anything outside the exact distribution allowlist before extracting.
while IFS= read -r member; do
  case "$member" in docker-compose.production.yml|database/schema_mysql.sql|docker/l2tp/options.xl2tpd|storage/keys/master_public_key.pem|runtime-release.env) ;; *) echo 'Unexpected release member' >&2; exit 1;; esac
done < <(tar -tzf "$stage/runtime.tar.gz")
[[ $(tar -tzf "$stage/runtime.tar.gz" | wc -l) -eq 5 ]] || { echo 'Incomplete runtime release' >&2; exit 1; }
tar --no-same-owner --no-same-permissions -xzf "$stage/runtime.tar.gz" -C "$stage"
# Image references come from the validated manifest, never from a mutable latest tag.
printf 'MAX_WEB_IMAGE=%s\nMAX_CORE_IMAGE=%s\nMAX_L2TP_IMAGE=%s\n' \
  "$(jq -r .images.web "$stage/release.json")" "$(jq -r .images.core "$stage/release.json")" "$(jq -r .images.l2tp "$stage/release.json")" > "$stage/runtime-release.env"
mkdir -p "$INSTALL_DIR"
[[ -f "$INSTALL_DIR/.env" ]] || touch "$INSTALL_DIR/.env"
compose=(docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" --env-file "$stage/runtime-release.env" -f "$stage/docker-compose.production.yml")
"${compose[@]}" config --quiet
"${compose[@]}" pull
# Check the actual image before replacing configuration or recreating any service.
web_image=$(jq -r .images.web "$stage/release.json")
docker run --rm --network none --entrypoint python "$web_image" /opt/max-protection/verify.py

echo '[3/6] Installing runtime configuration (preserving storage and database volumes)...'
backup="$INSTALL_DIR/.install-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup"
for file in docker-compose.production.yml runtime-release.env release.json .env; do
  [[ ! -f "$INSTALL_DIR/$file" ]] || cp -p -- "$INSTALL_DIR/$file" "$backup/"
done
install -Dm644 "$stage/docker-compose.production.yml" "$INSTALL_DIR/docker-compose.production.yml"
install -Dm644 "$stage/database/schema_mysql.sql" "$INSTALL_DIR/database/schema_mysql.sql"
install -Dm644 "$stage/docker/l2tp/options.xl2tpd" "$INSTALL_DIR/docker/l2tp/options.xl2tpd"
# Never replace a pre-existing public license key or any private instance keys.
if [[ ! -f "$INSTALL_DIR/storage/keys/master_public_key.pem" ]]; then
  install -Dm644 "$stage/storage/keys/master_public_key.pem" "$INSTALL_DIR/storage/keys/master_public_key.pem"
fi
install -m600 "$stage/runtime-release.env" "$INSTALL_DIR/runtime-release.env"
install -m600 "$stage/release.json" "$INSTALL_DIR/release.json"
mkdir -p "$INSTALL_DIR/storage/"{backups,uploads,logs,archive,l2tp}
touch "$INSTALL_DIR/storage/l2tp/chap-secrets" "$INSTALL_DIR/storage/l2tp/pap-secrets"
chmod 600 "$INSTALL_DIR/storage/l2tp/"*-secrets
# Bundles contain no Web/Core Python sources and no customer credentials.
# Existing source checkouts are not silently deleted; migrate them separately.

echo '[4/6] Configuring host networking...'
cat << 'EOF' > /etc/sysctl.d/99-maxradius.conf
net.ipv4.ip_forward = 1
net.ipv4.conf.all.forwarding = 1
net.ipv4.conf.default.forwarding = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 262144
net.core.wmem_default = 262144
net.core.netdev_max_backlog = 10000
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
EOF
sysctl --system -q 2>/dev/null || true
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  for port in 80/tcp 5090/tcp 1812/udp 1813/udp 37990/udp 1701/udp; do ufw allow "$port" >/dev/null; done
fi

echo '[5/6] Starting services...'
docker compose --project-directory "$INSTALL_DIR" --env-file "$INSTALL_DIR/.env" --env-file "$INSTALL_DIR/runtime-release.env" -f "$INSTALL_DIR/docker-compose.production.yml" up -d --wait --wait-timeout 240
# Preserve an unrelated/custom Nginx site instead of overwriting it.
if [[ $nginx_default_existed -eq 0 ]] || grep -q 'MAX RADIUS' /etc/nginx/sites-available/default; then
cat << 'NGINX_EOF' > /etc/nginx/sites-available/default
# MAX RADIUS Nginx Reverse Proxy Configuration (Port 80 -> Port 5090)
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 512M;
    client_body_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:5090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_connect_timeout 600s;
    }

    location /static/ {
        proxy_pass http://127.0.0.1:5090/static/;
        proxy_set_header Host $host;
        expires 7d;
        add_header Cache-Control "public, max-age=604800, immutable";
    }
}
NGINX_EOF

ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || true
systemctl enable nginx >/dev/null 2>&1 || true


fi
# Configure routing independently of the optional host HTTP proxy.
ip route replace 192.168.44.0/24 via 172.18.0.10 2>/dev/null || true

echo '[6/6] Verifying protected runtime...'
docker exec max_radius_web python /opt/max-protection/verify.py
SERVER_IP=$(hostname -I | awk '{print $1}')
printf '\nMAX RADIUS release %s installed. Dashboard: http://%s:5090\n' "$release" "$SERVER_IP"
printf 'Existing storage, credentials and database volumes were preserved.\n'
printf 'Initial administrator credentials apply only to a fresh database; change them after first login.\n'
