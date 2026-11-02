"""Display-only accounting metadata; no router calls or package inference."""
from services.sales_source_service import packet_service


def index_live_sessions(rows):
    """Index already heartbeat-filtered rows once, across all NAS in the query scope."""
    latest = {}
    counts = {}
    for row in rows or ():
        username = str(row.get('username') or '').lower()
        if not username:
            continue
        latest[username] = row
        counts[username] = counts.get(username, 0) + 1
    return latest, counts


def connection_point_label(server, kind='', interface='', separator=' · '):
    """Show recorded names independently of optional protocol metadata."""
    server = str(server or '').strip()
    interface = str(interface or '').strip()
    if not server or kind == 'conflict' or any(ord(c) < 32 or ord(c) == 127 for c in server):
        return 'غير محدد'
    if kind not in ('hotspot', 'pppoe'):
        return server
    parts = ['Hotspot' if kind == 'hotspot' else 'PPPoE', server]
    if kind == 'pppoe' and interface and interface != server and not any(ord(c) < 32 or ord(c) == 127 for c in interface):
        parts.append(interface)
    return separator.join(parts)


def enrich_session_display(rows, query):
    addresses = sorted({str(row.get('nasipaddress') or '') for row in rows} - {''})
    names = {}
    if addresses:
        placeholders = ','.join('?' for _ in addresses)
        catalog = query('''SELECT address,label FROM (
            SELECT ip_address address,name label,0 priority,id FROM wisp_nas_devices
            UNION ALL SELECT nasname,shortname,1,id FROM nas) catalog
            WHERE address IN (''' + placeholders + ') ORDER BY priority,id', tuple(addresses))
        for item in catalog:
            label = str(item.get('label') or '').strip()
            if label:
                names.setdefault(str(item['address']), label)
    for row in rows:
        address = str(row.get('nasipaddress') or '')
        row['nas_display_name'] = names.get(address) or address or 'غير محدد'
        kind = packet_service(row)
        row['connection_point'] = connection_point_label(
            row.get('calledstationid'), kind, row.get('nasportid'))
    return rows
