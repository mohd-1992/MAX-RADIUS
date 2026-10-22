"""Build-only template embedding; compile the generated module before shipping.

This makes templates harder to edit, not secret: rendered HTML reaches browsers.
Source templates stay in the checkout; only the disposable build tree is changed.
"""
from pathlib import Path
import base64
import json
import zlib


def embed(root):
    root = Path(root)
    folder = root / 'web' / 'templates'
    templates = {p.relative_to(folder).as_posix(): p.read_text(encoding='utf-8-sig')
                 for p in sorted(folder.rglob('*.html'))}
    if not templates:
        raise ValueError('No templates to embed')
    target = root / 'web' / '_embedded_templates.py'
    payload = base64.b85encode(zlib.compress(json.dumps(templates, ensure_ascii=False).encode('utf-8'), 9))
    target.write_text('"""Generated release templates; not encryption or a confidentiality boundary."""\n'
                      'import base64, json, zlib\nfrom jinja2 import DictLoader\n'
                      '_PAYLOAD = ' + repr(payload) + '\n\n'
                      'def template_loader():\n'
                      '    return DictLoader(json.loads(zlib.decompress(base64.b85decode(_PAYLOAD))))\n', encoding='utf-8')
    return templates


if __name__ == '__main__':
    embed('/app')
