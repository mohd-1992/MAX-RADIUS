#!/usr/bin/env bash
# ==============================================================================
# MAX RADIUS - Automated One-Line Production Installer for Ubuntu / Debian
# GitHub: https://github.com/mohd-1992/MAX-RADIUS
# ==============================================================================

set -e

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

TOKEN="${1:-$GITHUB_TOKEN}"
if [ -n "$TOKEN" ]; then
  REPO_URL="https://${TOKEN}@github.com/mohd-1992/MAX-RADIUS.git"
else
  REPO_URL="https://github.com/mohd-1992/MAX-RADIUS.git"
fi

INSTALL_DIR="/opt/max-radius"
BRANCH="main"

echo -e "${CYAN}${BOLD}"
echo "=================================================================="
echo "    __  ______  _  __   ____  ___    ____  Extended Engine        "
echo "   /  |/  /   | | |/ /  / __ \/   |  / __ \/  _/ / / / ___/       "
echo "  / /|_/ / /| | |   /  / /_/ / /| | / / / // / / / / /\__ \        "
echo " / /  / / ___ |/   |  / _, _/ ___ |/ /_/ // / / /_/ /___/ /        "
echo "/_/  /_/_/  |_/_/|_| /_/ |_/_/  |_/_____/___/ \____//____/         "
echo "                                                                  "
echo "       Next-Gen FreeRADIUS & MikroTik WISP ISP Platform           "
echo "=================================================================="
echo -e "${NC}"

# Check Root Privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run this installation script as root (sudo bash).${NC}"
  exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS=$ID
  OS_VER=$VERSION_ID
else
  echo -e "${RED}[ERROR] Cannot detect Linux distribution. Supported: Ubuntu 20.04+, Debian 11+${NC}"
  exit 1
fi

echo -e "${BLUE}[1/6] Detected OS: ${BOLD}${OS} ${OS_VER}${NC}"

# Safely handle Ubuntu unattended background upgrades and lock conflicts
echo -e "${BLUE}[2/6] Preparing package manager & unlocking apt...${NC}"
systemctl stop unattended-upgrades.service 2>/dev/null || true
systemctl stop apt-daily.service 2>/dev/null || true
systemctl stop apt-daily-upgrade.service 2>/dev/null || true

# Kill any lingering background apt/dpkg lock holders if active
killall -q -9 unattended-upgr apt apt-get dpkg 2>/dev/null || true
sleep 1

# Remove any stale lock files safely
rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>/dev/null || true
dpkg --configure -a 2>/dev/null || true

export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q curl wget git ufw jq unzip openssl ca-certificates gnupg lsb-release

# Install Docker & Docker Compose Plugin if not installed
echo -e "${BLUE}[3/6] Checking Docker & Docker Compose installation...${NC}"
if ! command -v docker &> /dev/null; then
  echo -e "${YELLOW}[INFO] Docker not found. Installing Docker Engine...${NC}"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo -e "${GREEN}[OK] Docker is already installed: $(docker --version)${NC}"
fi

# Add invoking non-root user to docker group if applicable
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
  usermod -aG docker "$SUDO_USER" 2>/dev/null || true
fi

# Ensure Kernel Parameters for High-Throughput RADIUS & L2TP
echo -e "${BLUE}[4/6] Optimizing Kernel Parameters & IP Forwarding...${NC}"
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

# Prepare Installation Directory & Clone Repository
echo -e "${BLUE}[5/6] Deploying MAX RADIUS codebase into ${INSTALL_DIR}...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
  echo -e "${YELLOW}[INFO] Updating existing installation...${NC}"
  cd "$INSTALL_DIR"
  git fetch --all
  git reset --hard "origin/$BRANCH"
else
  if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}[INFO] Backing up existing directory...${NC}"
    mv "$INSTALL_DIR" "${INSTALL_DIR}_backup_$(date +%s)"
  fi
  git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

# Ensure storage directories exist with proper permissions
mkdir -p "$INSTALL_DIR/storage/backups" "$INSTALL_DIR/storage/keys" "$INSTALL_DIR/storage/uploads" "$INSTALL_DIR/storage/logs" "$INSTALL_DIR/storage/archive" "$INSTALL_DIR/storage/l2tp" "$INSTALL_DIR/data"
touch "$INSTALL_DIR/storage/l2tp/chap-secrets" "$INSTALL_DIR/storage/l2tp/pap-secrets" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/storage/l2tp/"*-secrets 2>/dev/null || true
chmod -R 777 "$INSTALL_DIR/storage"

# Create .env if not present
if [ ! -f "$INSTALL_DIR/.env" ]; then
  if [ -f "$INSTALL_DIR/env.example" ]; then
    cp "$INSTALL_DIR/env.example" "$INSTALL_DIR/.env"
  fi
fi

# Open Required Ports in UFW if active
if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
  echo -e "${YELLOW}[INFO] Configuring UFW firewall rules for RADIUS, Web, and L2TP...${NC}"
  ufw allow 80/tcp comment "MAX RADIUS Web HTTP" 2>/dev/null || true
  ufw allow 5090/tcp comment "MAX RADIUS Web Direct" 2>/dev/null || true
  ufw allow 1812/udp comment "FreeRADIUS Auth" 2>/dev/null || true
  ufw allow 1813/udp comment "FreeRADIUS Acct" 2>/dev/null || true
  ufw allow 37990/udp comment "FreeRADIUS CoA/PoD" 2>/dev/null || true
  ufw allow 1701/udp comment "L2TP VPN Server" 2>/dev/null || true
  ufw allow 500/udp comment "IPSec IKE" 2>/dev/null || true
  ufw allow 4500/udp comment "IPSec NAT-T" 2>/dev/null || true
fi

# Launch Docker Containers
echo -e "${BLUE}[6/6] Pulling images and starting MAX RADIUS services...${NC}"
docker compose pull
docker compose up -d

# Configure Host Kernel Routing for L2TP Subnet
ip route replace 192.168.44.0/24 via 172.18.0.10 2>/dev/null || true

# Verify Database Schema Integrity
echo -e "${BLUE}[+] Verifying database integrity...${NC}"
for i in {1..25}; do
  if docker exec max_radius_db mariadb-admin ping -h 127.0.0.1 -u root -prootpass &>/dev/null; then
    break
  fi
  sleep 1
done

docker exec max_radius_db mariadb -u root -prootpass radius_wisp -e "
  SET FOREIGN_KEY_CHECKS=0;
  SOURCE /docker-entrypoint-initdb.d/01_schema.sql;
  SET FOREIGN_KEY_CHECKS=1;
" 2>/dev/null || true

# Get Public Server IP
SERVER_IP=$(curl -s -m 2 https://api.ipify.org || curl -s -m 2 https://ifconfig.me || hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}${BOLD}==================================================================${NC}"
echo -e "${GREEN}${BOLD}  🎉  MAX RADIUS Platform Installed & Started Successfully!       ${NC}"
echo -e "${GREEN}${BOLD}==================================================================${NC}"
echo ""
echo -e "  🌐 ${BOLD}Web Dashboard:${NC}    http://${SERVER_IP}:5090  or  http://${SERVER_IP}"
echo -e "  👤 ${BOLD}Default Admin:${NC}    admin"
echo -e "  🔑 ${BOLD}Default Pass:${NC}     admin"
echo ""
echo -e "  🔒 ${BOLD}FreeRADIUS Auth:${NC}  Port 1812 / UDP (Secret: max123)"
echo -e "  📊 ${BOLD}FreeRADIUS Acct:${NC}  Port 1813 / UDP (Secret: max123)"
echo -e "  ⚡ ${BOLD}CoA / PoD Port:${NC}   Port 37990 / UDP"
echo -e "  🛡️ ${BOLD}L2TP VPN Server:${NC}  Port 1701 / UDP"
echo ""
echo -e "  📂 ${BOLD}Install Location:${NC} ${INSTALL_DIR}"
echo -e "  📋 ${BOLD}Logs & Status:${NC}    cd ${INSTALL_DIR} && docker compose ps"
echo ""
echo -e "${CYAN}Tip: Change the default admin password upon initial login.${NC}"
echo -e "${GREEN}==================================================================${NC}"
