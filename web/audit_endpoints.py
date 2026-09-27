import os
import re
import sys

sys.path.insert(0, '/app')
from web.app import app

valid_endpoints = set(app.view_functions.keys())
valid_endpoints.add('static')

template_dir = '/app/web/templates'
missing_endpoints = []
all_found = []

for root, _, files in os.walk(template_dir):
    for f in files:
        if f.endswith('.html'):
            filepath = os.path.join(root, f)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as fp:
                content = fp.read()
            matches = re.findall(r"url_for\s*\(\s*['\"]([^'\"]+)['\"]", content)
            rel_path = os.path.relpath(filepath, template_dir)
            for ep in matches:
                all_found.append((rel_path, ep))
                if ep not in valid_endpoints:
                    missing_endpoints.append((rel_path, ep))

print(f"Total url_for references checked: {len(all_found)}")
print(f"Invalid/Missing url_for endpoints: {len(missing_endpoints)}")
print("=" * 70)
for tpl, ep in sorted(set(missing_endpoints)):
    print(f"Template: {tpl:<40} | Missing endpoint: '{ep}'")
