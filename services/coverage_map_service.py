"""
services/coverage_map_service.py
--------------------------------
Live Network Coverage, Towers & Access Points (Sectors) GIS Engine for MAX RADIUS.
Manages geographical tower coordinates, sectors/access points, live subscribers per site,
and telemetry.
"""

from database.db import get_connection, query_all, query_one, execute_write

_get_db = get_connection

def ensure_gis_tables():
    """Ensure coordinates, tower location metadata, and access points exist."""
    db = _get_db()
    try:
        with db.cursor() as cur:
            # 1. Towers / Sites table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_tower_locations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nas_id INT NULL,
                    nas_ip VARCHAR(64) NULL,
                    tower_name VARCHAR(128) NOT NULL,
                    latitude DECIMAL(10,8) NOT NULL DEFAULT 15.369445,
                    longitude DECIMAL(11,8) NOT NULL DEFAULT 44.191006,
                    coverage_radius_meters INT DEFAULT 500,
                    tower_type ENUM('hotspot', 'fiber_olt', 'pppoe_tower', 'relay', 'backhaul') DEFAULT 'hotspot',
                    notes VARCHAR(255) NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_tower_nas (nas_id),
                    INDEX idx_tower_ip (nas_ip)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
            
            # Safe schema migration for pre-existing tables
            try:
                cur.execute("ALTER TABLE wisp_tower_locations ADD COLUMN nas_id INT NULL AFTER id")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE wisp_tower_locations MODIFY COLUMN nas_ip VARCHAR(64) NULL")
            except Exception:
                pass

            # 2. Access Points / Sectors table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wisp_access_points (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tower_id INT NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    device_model VARCHAR(80) DEFAULT 'Ubiquiti Rocket',
                    ip_address VARCHAR(64) NULL,
                    snmp_community VARCHAR(64) DEFAULT 'public',
                    interface_name VARCHAR(64) NULL,
                    frequency_mhz INT DEFAULT 5800,
                    azimuth_deg INT DEFAULT 0,
                    beamwidth_deg INT DEFAULT 90,
                    ssid VARCHAR(128) NULL,
                    coverage_radius_meters INT DEFAULT 400,
                    status ENUM('active', 'maintenance', 'offline') DEFAULT 'active',
                    notes VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_ap_tower (tower_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # Auto-populate towers for known NAS routers if empty
            cur.execute("SELECT COUNT(*) as cnt FROM wisp_tower_locations")
            if (cur.fetchone() or {}).get('cnt', 0) == 0:
                cur.execute("""
                    INSERT IGNORE INTO wisp_tower_locations (nas_ip, tower_name, latitude, longitude, coverage_radius_meters, tower_type)
                    SELECT 
                        nasname, 
                        COALESCE(shortname, nasname), 
                        15.369445 + (id * 0.005), 
                        44.191006 + (id * 0.005), 
                        600, 
                        'hotspot'
                    FROM nas
                """)
            db.commit()
    finally:
        db.close()

def get_coverage_map_data():
    """Get all towers with their attached access points, live active subscriber counts and status."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT 
                    t.id as tower_id,
                    t.nas_id,
                    t.nas_ip,
                    t.tower_name,
                    t.latitude,
                    t.longitude,
                    t.coverage_radius_meters,
                    t.tower_type,
                    t.notes,
                    COALESCE((SELECT COUNT(*) FROM radacct WHERE nasipaddress = CONVERT(t.nas_ip USING utf8mb4) COLLATE utf8mb4_general_ci AND acctstoptime IS NULL), 0) as active_users,
                    COALESCE((SELECT SUM(acctinputoctets + acctoutputoctets) FROM radacct WHERE nasipaddress = CONVERT(t.nas_ip USING utf8mb4) COLLATE utf8mb4_general_ci AND acctstoptime IS NULL), 0) as traffic_bytes
                FROM wisp_tower_locations t
                ORDER BY t.id ASC
            """)
            raw_towers = cur.fetchall()

            # Fetch all access points
            cur.execute("""
                SELECT ap.*, t.tower_name 
                FROM wisp_access_points ap
                JOIN wisp_tower_locations t ON ap.tower_id = t.id
                ORDER BY ap.id ASC
            """)
            raw_aps = cur.fetchall()

            aps_by_tower = {}
            for ap in raw_aps:
                tid = ap['tower_id']
                if tid not in aps_by_tower:
                    aps_by_tower[tid] = []
                aps_by_tower[tid].append({
                    'id': ap['id'],
                    'tower_id': ap['tower_id'],
                    'name': ap['name'],
                    'device_model': ap.get('device_model') or 'Ubiquiti',
                    'ip_address': ap.get('ip_address') or '',
                    'interface_name': ap.get('interface_name') or '',
                    'frequency_mhz': int(ap.get('frequency_mhz') or 5800),
                    'azimuth_deg': int(ap.get('azimuth_deg') or 0),
                    'beamwidth_deg': int(ap.get('beamwidth_deg') or 90),
                    'ssid': ap.get('ssid') or '',
                    'coverage_radius_meters': int(ap.get('coverage_radius_meters') or 400),
                    'status': ap.get('status') or 'active',
                    'notes': ap.get('notes') or ''
                })

            towers = []
            for t in raw_towers:
                tid = int(t.get('tower_id', 0))
                tower_aps = aps_by_tower.get(tid, [])
                towers.append({
                    'tower_id': tid,
                    'nas_id': t.get('nas_id'),
                    'nas_ip': str(t.get('nas_ip') or ''),
                    'tower_name': str(t.get('tower_name', '')),
                    'latitude': float(t.get('latitude', 15.369445)),
                    'longitude': float(t.get('longitude', 44.191006)),
                    'coverage_radius_meters': int(t.get('coverage_radius_meters', 500)),
                    'tower_type': str(t.get('tower_type', 'hotspot')),
                    'notes': t.get('notes') or '',
                    'active_users': int(t.get('active_users', 0)),
                    'traffic_bytes': int(t.get('traffic_bytes', 0)),
                    'access_points': tower_aps,
                    'ap_count': len(tower_aps)
                })

            total_towers = len(towers)
            total_aps = len(raw_aps)
            total_active_users = sum(t.get('active_users', 0) for t in towers)
            total_traffic_bytes = sum(t.get('traffic_bytes', 0) for t in towers)

        return {
            'total_towers': total_towers,
            'total_aps': total_aps,
            'total_active_users': total_active_users,
            'total_traffic_bytes': total_traffic_bytes,
            'towers': towers
        }
    finally:
        db.close()

# ----------------- Towers CRUD -----------------
def create_tower(tower_name, latitude, longitude, coverage_radius_meters=500, tower_type='hotspot', nas_id=None, nas_ip=None, notes=''):
    """Create a new Tower / Site."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO wisp_tower_locations (tower_name, latitude, longitude, coverage_radius_meters, tower_type, nas_id, nas_ip, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (tower_name, float(latitude), float(longitude), int(coverage_radius_meters), tower_type, nas_id or None, nas_ip or None, notes))
            db.commit()
            return True, "تمت إضافة البرج بنجاح."
    except Exception as e:
        return False, f"فشل في إضافة البرج: {str(e)}"
    finally:
        db.close()

def update_tower_location(tower_id, lat, lng, radius, tower_name, tower_type='hotspot', nas_id=None, nas_ip=None, notes=''):
    """Update coordinates & info of a specific tower."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_tower_locations 
                SET latitude = %s, longitude = %s, coverage_radius_meters = %s, tower_name = %s, tower_type = %s,
                    nas_id = %s, nas_ip = %s, notes = %s
                WHERE id = %s
            """, (float(lat), float(lng), int(radius), tower_name, tower_type, nas_id or None, nas_ip or None, notes, int(tower_id)))
            db.commit()
            return True, "تم تحديث بيانات وموقع البرج بنجاح."
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def delete_tower(tower_id):
    """Delete a tower and all its linked access points."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM wisp_access_points WHERE tower_id = %s", (int(tower_id),))
            cur.execute("DELETE FROM wisp_tower_locations WHERE id = %s", (int(tower_id),))
            db.commit()
            return True, "تم حذف البرج وكافة أجهزة البث التابعة له بنجاح."
    except Exception as e:
        return False, f"فشل في حذف البرج: {str(e)}"
    finally:
        db.close()

# ----------------- Access Points CRUD -----------------
def create_access_point(tower_id, name, device_model='Ubiquiti Rocket', ip_address='', snmp_community='public', interface_name='', frequency_mhz=5800, azimuth_deg=0, beamwidth_deg=90, ssid='', coverage_radius_meters=400, status='active', notes=''):
    """Create a new Access Point / Sector under a specific tower."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO wisp_access_points (tower_id, name, device_model, ip_address, snmp_community, interface_name, frequency_mhz, azimuth_deg, beamwidth_deg, ssid, coverage_radius_meters, status, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (int(tower_id), name, device_model, ip_address or None, snmp_community or 'public', interface_name or None, int(frequency_mhz or 5800), int(azimuth_deg or 0), int(beamwidth_deg or 90), ssid or None, int(coverage_radius_meters or 400), status or 'active', notes or ''))
            db.commit()
            return True, "تمت إضافة جهاز البث بنجاح."
    except Exception as e:
        return False, f"فشل في إضافة جهاز البث: {str(e)}"
    finally:
        db.close()

def update_access_point(ap_id, name, device_model='Ubiquiti Rocket', ip_address='', snmp_community='public', interface_name='', frequency_mhz=5800, azimuth_deg=0, beamwidth_deg=90, ssid='', coverage_radius_meters=400, status='active', notes=''):
    """Update an existing Access Point."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE wisp_access_points
                SET name = %s, device_model = %s, ip_address = %s, snmp_community = %s, interface_name = %s,
                    frequency_mhz = %s, azimuth_deg = %s, beamwidth_deg = %s, ssid = %s, coverage_radius_meters = %s,
                    status = %s, notes = %s
                WHERE id = %s
            """, (name, device_model, ip_address or None, snmp_community or 'public', interface_name or None, int(frequency_mhz or 5800), int(azimuth_deg or 0), int(beamwidth_deg or 90), ssid or None, int(coverage_radius_meters or 400), status or 'active', notes or '', int(ap_id)))
            db.commit()
            return True, "تم تحديث بيانات جهاز البث بنجاح."
    except Exception as e:
        return False, f"فشل في تحديث جهاز البث: {str(e)}"
    finally:
        db.close()

def delete_access_point(ap_id):
    """Delete an access point."""
    ensure_gis_tables()
    db = _get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM wisp_access_points WHERE id = %s", (int(ap_id),))
            db.commit()
            return True, "تم حذف جهاز البث بنجاح."
    except Exception as e:
        return False, f"فشل في حذف جهاز البث: {str(e)}"
    finally:
        db.close()

def get_tower_access_points(tower_id):
    """Get all APs for a given tower ID."""
    ensure_gis_tables()
    return query_all("SELECT * FROM wisp_access_points WHERE tower_id = ? ORDER BY id ASC", (tower_id,))
