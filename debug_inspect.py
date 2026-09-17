
import gzip
import re

backup_path = '/app/data/migration_cache/uploaded_backup.gz'
print("Inspecting backup file:", backup_path)

with gzip.open(backup_path, 'rt', encoding='utf-8', errors='ignore') as f:
    tables = {}
    users_samples = []
    radacct_samples = []
    for line_no, line in enumerate(f, 1):
        if 'INSERT INTO' in line:
            val_idx = line.find(' VALUES (')
            if val_idx != -1:
                t_sub = line[:val_idx]
                m_t = re.search(r'INSERT INTO `?([a-zA-Z0-9_]+)`?', t_sub)
                if m_t:
                    tname = m_t.group(1).lower()
                    tables[tname] = tables.get(tname, 0) + 1
                    if tname == 'users':
                        users_samples.append((line_no, len(line)))
                    elif tname.startswith('radacct'):
                        radacct_samples.append((line_no, len(line)))

print("TABLES FOUND:")
for t, cnt in sorted(tables.items()):
    print(f"  {t}: {cnt} INSERT lines")

print(f"Users lines count: {len(users_samples)}, sample lines: {users_samples[:5]}")
print(f"Radacct lines count: {len(radacct_samples)}, sample lines: {radacct_samples[:5]}")
