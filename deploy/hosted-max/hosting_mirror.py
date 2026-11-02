"""Identity-preserving panel mirrors when a proven deleted DNS name is reused."""
import json


def sync(db, rows, encrypt, retired):
    for row in rows:
        previous = db.execute('SELECT id,name,domain FROM tenants WHERE domain=?', (row['domain'],)).fetchone()
        if previous and previous['id'] != row['id']:
            proof = retired(row['slug'], previous['id'])
            if (proof.get('deleted') is not True or proof.get('tenant_id') != previous['id']
                    or proof.get('domain') != row['domain']):
                raise ValueError('Existing panel identity was not proven deleted; no identity replaced')
            db.execute('''CREATE TABLE IF NOT EXISTS hosting_retired_mirrors(
                id TEXT PRIMARY KEY,domain TEXT NOT NULL,name TEXT NOT NULL,replacement TEXT NOT NULL,
                proof TEXT NOT NULL,retired_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
            db.execute('INSERT OR IGNORE INTO hosting_retired_mirrors(id,domain,name,replacement,proof) VALUES(?,?,?,?,?)',
                       (previous['id'], previous['domain'], previous['name'], row['id'], json.dumps(proof)))
            # Keep the old ID, encrypted secret, and all foreign keys. The ledger
            # retains its real historical domain; the unique live name is released.
            db.execute('UPDATE tenants SET domain=? WHERE id=? AND domain=?',
                       ('retired-' + previous['id'] + '.invalid', previous['id'], previous['domain']))
        db.execute('INSERT INTO tenants(id,name,domain,username,secret) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET name=excluded.name,domain=excluded.domain',
                   (row['id'], row['name'], row['domain'], 'admin', encrypt('')))
