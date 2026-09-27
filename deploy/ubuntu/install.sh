#!/usr/bin/env bash
# ==============================================================================
# 🚀 MAX RADIUS - المثبت الصامت الشامل لسيرفر أوبنتو 24.04.4 LTS
# Fully Automated Unattended Production Installer for Ubuntu 24.04 LTS
# ==============================================================================

set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

# الألوان
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}❌ يجب تشغيل هذا السكربت بصلاحيات الجذر (root / sudo).${NC}"
    exit 1
fi

echo -e "${CYAN}${BOLD}"
echo "===================================================================="
echo "    🚀 جاري تثبيت منظومة MAX RADIUS المتكاملة على Ubuntu 24.04 LTS"
echo "    📦 التثبيت صامت بالكامل وبدون الحاجة لأي تدخل يدوي..."
echo "===================================================================="
echo -e "${NC}"

INSTALL_DIR="/opt/max-radius"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# 1. تحديث النظام وتثبيت الحزم الأساسية بصمت
echo -e "${YELLOW}[1/7] 📦 تحديث مستودعات النظام وتثبيت الحزم الأساسية...${NC}"
apt-get update -y -q > /dev/null
apt-get install -y -q --no-install-recommends \
    curl \
    wget \
    git \
    nginx \
    ufw \
    net-tools \
    iproute2 \
    ca-certificates \
    gnupg \
    lsb-release \
    iptables \
    mariadb-client > /dev/null

# 2. تثبيت Docker Engine و Docker Compose الحديث بصمت
echo -e "${YELLOW}[2/7] 🐳 التحقق من محرك Docker وتثبيته...${NC}"
if ! command -v docker &> /dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update -y -q > /dev/null
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin > /dev/null
fi

# 3. ضبط إعدادات Docker Daemon للحفاظ على الـ Real Source IP
echo -e "${YELLOW}[3/7] ⚙️  ضبط معمارية كيرنل لينكس لحفظ الـ IP الحقيقي للراوترات...${NC}"
mkdir -p /etc/docker
cat << 'EOF' > /etc/docker/daemon.json
{
  "userland-proxy": false,
  "iptables": true,
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
EOF
systemctl daemon-reload
systemctl restart docker
systemctl enable docker > /dev/null 2>&1

# 4. نسخ ملفات المنظومة إلى /opt/max-radius
echo -e "${YELLOW}[4/7] 📂 تجهيز ملفات المنظومة وقواعد البيانات في ${INSTALL_DIR}...${NC}"
mkdir -p "${INSTALL_DIR}"
if [ -d "${PROJECT_ROOT}/core" ]; then
    cp -r "${PROJECT_ROOT}"/* "${INSTALL_DIR}/" 2>/dev/null || true
fi

mkdir -p "${INSTALL_DIR}/storage/l2tp" "${INSTALL_DIR}/storage/keys" "${INSTALL_DIR}/storage/backups" "${INSTALL_DIR}/storage/uploads"
touch "${INSTALL_DIR}/storage/l2tp/chap-secrets" "${INSTALL_DIR}/storage/l2tp/pap-secrets" 2>/dev/null || true
chmod 600 "${INSTALL_DIR}/storage/l2tp/"*-secrets 2>/dev/null || true
chmod -R 777 "${INSTALL_DIR}/storage" 2>/dev/null || true

cd "${INSTALL_DIR}"

# 5. ضبط Nginx Reverse Proxy لفتح النظام مباشرة على المنفذ 80 (بدون بورت)
echo -e "${YELLOW}[5/7] 🌐 إعداد خادم Nginx لربط لوحة التحكم بالمنفذ 80 مباشرة...${NC}"
cp -f "${INSTALL_DIR}/deploy/ubuntu/nginx.conf" /etc/nginx/sites-available/default
nginx -t > /dev/null 2>&1
systemctl restart nginx
systemctl enable nginx > /dev/null 2>&1

# ضبط الجدار الناري UFW
ufw allow 80/tcp > /dev/null 2>&1 || true
ufw allow 443/tcp > /dev/null 2>&1 || true
ufw allow 1812/udp > /dev/null 2>&1 || true
ufw allow 1813/udp > /dev/null 2>&1 || true
ufw allow 3799/udp > /dev/null 2>&1 || true
ufw allow 22/tcp > /dev/null 2>&1 || true

# 6. تثبيت أداة maxip وضبط الصلاحيات
echo -e "${YELLOW}[6/7] 🛠️  تثبيت أداة ضبط الشبكة (maxip)...${NC}"
cp -f "${INSTALL_DIR}/deploy/ubuntu/maxip" /usr/local/bin/maxip
chmod +x /usr/local/bin/maxip

# إنشاء خدمة Systemd للتشغيل التلقائي عند إقلاع السيرفر
cat << EOF > /etc/systemd/system/max-radius.service
[Unit]
Description=MAX RADIUS Enterprise WISP System
After=docker.service network.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable max-radius.service > /dev/null 2>&1

# 7. بناء وتشغيل الحاويات وتطبيق بيانات المدير الافتراضي (max / max123)
echo -e "${YELLOW}[7/7] 🚀 تشغيل حاويات السيرفر (MariaDB + FreeRADIUS + Web App)...${NC}"
docker compose down > /dev/null 2>&1 || true
docker compose up -d --build

# الانتظار حتى تصبح قاعدة البيانات جاهزة
echo -e "${CYAN}⏳ جاري تهيئة قاعدة البيانات وضبط الحساب الافتراضي (max / max123)...${NC}"
for i in {1..30}; do
    if docker exec max_radius_db mariadb -uradius -pradpass radius_wisp -e "SELECT 1" > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# حقن حساب المدير max / max123
docker exec -i max_radius_db mariadb -uradius -pradpass radius_wisp < "${INSTALL_DIR}/deploy/ubuntu/init_admin.sql" 2>/dev/null || true

# استخراج الـ IP الحالي للسيرفر
CURRENT_IP=$(ip -4 addr show $(ip route | grep default | awk '{print $5}' | head -n1) 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1 || echo "127.0.0.1")

echo ""
echo -e "${GREEN}${BOLD}====================================================================${NC}"
echo -e "${GREEN}${BOLD}   🎉 تم تثبيت وتشغيل منظومة MAX RADIUS بنجاح تام على أوبنتو!${NC}"
echo -e "${GREEN}${BOLD}====================================================================${NC}"
echo ""
echo -e "  🌐 ${BOLD}رابط لوحة التحكم المباشر:${NC}  ${CYAN}${BOLD}http://${CURRENT_IP}${NC} (بدون كتابة بورت 5090)"
echo -e "  👤 ${BOLD}اسم المستخدم الافتراضي:${NC}  ${YELLOW}${BOLD}max${NC}"
echo -e "  🔑 ${BOLD}كلمة المرور الافتراضية:${NC}  ${YELLOW}${BOLD}max123${NC}"
echo ""
echo -e "  🛠️  ${BOLD}لتغيير وضبط عنوان IP السيرفر التفاعلي في أي وقت:${NC}"
echo -e "      فقط اكتب في التيرمينال الأمر: ${GREEN}${BOLD}maxip${NC}"
echo ""
echo "===================================================================="
