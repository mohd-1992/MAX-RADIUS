# دليل المطور ومرجع المشروع
ابدأ بـ [خريطة المشروع](docs/PROJECT_MAP.md)، [إعداد التطوير](docs/DEVELOPMENT.md)، و[تعليمات العملاء البرمجيين](AGENTS.md).
مرجع البنية والقواعد والبيانات والواجهة والنشر والفحوص مرتبط بالخريطة. [الحالة المسجلة](docs/CURRENT_STATE.md) توضح آخر نتائج مؤكدة وحدودها.

الأداة المحلية: `python scripts/project_tools.py doctor` ثم `python scripts/project_tools.py index` و`python scripts/project_tools.py find <feature>`.
المهارات المتخصصة في `.agents/skills/`. لا يلزم تحميل جميع الوثائق في كل مهمة.

---

# MAX RADIUS - Next-Generation FreeRADIUS & MikroTik WISP ISP Platform 🚀

[![Website](https://img.shields.io/badge/Official%20Website-Live%20Demo-cyan.svg?style=flat&logo=google-chrome)](https://mohd-1992.github.io/MAX-RADIUS/)
[![Docker Pulls](https://img.shields.io/badge/Docker-Hub-blue.svg?logo=docker)](https://hub.docker.com/u/mohd777)
[![FreeRADIUS](https://img.shields.io/badge/FreeRADIUS-3.2-orange.svg)](https://freeradius.org/)
[![MikroTik RouterOS](https://img.shields.io/badge/MikroTik-RouterOS%20v6%20%2F%20v7-red.svg)](https://mikrotik.com/)
[![License](https://img.shields.io/badge/License-Proprietary-green.svg)]()

🌐 **الموقع الرسمي والدليل التفاعلي**: [https://mohd-1992.github.io/MAX-RADIUS/](https://mohd-1992.github.io/MAX-RADIUS/)

MAX RADIUS is a carrier-grade, cloud-native Authentication, Authorization, and Accounting (AAA) & Billing platform built specifically for Wireless Internet Service Providers (WISP), Hotspot networks, and FTTH/Broadband operators.

---

## ⚡ Quick One-Line Automated Installation (Ubuntu 20.04 / 22.04 / 24.04)

Deploy a complete production instance in under **2 minutes**:

```bash
curl -sSL https://raw.githubusercontent.com/mohd-1992/MAX-RADIUS/main/install.sh | sudo bash
```

---

## تحديث صور الحاويات

سحب المستودع بـ `git pull` يحدّث الملفات فقط. لتحديث صور تثبيت الإنتاج القائم، من مجلد التثبيت وبنفس ملفات Compose وoverride المستخدمة فيه:

```bash
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
```

سكربتا `install.sh` و`scripts/install.sh` ينفذان السحب قبل التشغيل بالفعل. خدمة Autoheal تستخدم `pull_policy: always` في ملفّي Compose لفحص صورتها المنشورة عند تشغيل الخدمة أيضًا؛ فشل الوصول إلى Registry قد يمنع إكمال التشغيل. هذه السياسة لا تشغّل تحديثًا دوريًا تلقائيًا.

## 🌟 Key Architecture & Capabilities

- **High-Performance FreeRADIUS Core**:
  - Sub-millisecond CHAP, PAP, and MS-CHAPv2 authentication.
  - Native dynamic quota countdown with Gigawords rollover (`Mikrotik-Total-Limit` & `Mikrotik-Total-Limit-Gigawords`).
  - Arabic localized rejection messages mapped directly to MikroTik hotspot and user login screens.
- **WISP & Reseller Hierarchy**:
  - Multi-tier reseller wallets, batch generator, custom card templates, and POS integration.
  - Granular RBAC permissions engine.
- **Ultra-Fast Database Migration Engine**:
  - Direct import from SAS4 and external MySQL radius databases (150,000+ records processed in sub-15 seconds).
- **Embedded L2TP / IPsec VPN Concentrator**:
  - Encrypted backhaul tunnels for connecting remote MikroTik routers over dynamic IPs / NAT.
- **Automated Health & Defragmentation**:
  - Engineering database audit and self-healing schema manager.

---

## 🛠️ Manual Deployment via Docker Compose

```bash
git clone https://github.com/mohd-1992/MAX-RADIUS.git /opt/max-radius
cd /opt/max-radius
cp env.example .env
docker compose -f docker-compose.production.yml pull
docker compose -f docker-compose.production.yml up -d
```

---

## 🌐 Default Ports & Credentials

| Service | Port / Protocol | Default Secret / Credentials |
| :--- | :--- | :--- |
| **Web Dashboard** | `80` & `5090` / TCP | `admin` / `admin123` |
| **FreeRADIUS Auth** | `1812` / UDP | `max123` |
| **FreeRADIUS Acct** | `1813` / UDP | `max123` |
| **FreeRADIUS CoA/PoD** | `37990` / UDP | `max123` |
| **L2TP VPN Server** | `1701` / UDP | Configured per tunnel |
