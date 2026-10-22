"""Build-only conservative minification of first-party static assets.

Do not rewrite vendor notices, identifiers, HTML attributes or network behaviour.
"""
from pathlib import Path


def minify(root):
    from rjsmin import jsmin
    from rcssmin import cssmin
    for path in (Path(root) / 'web' / 'static').rglob('*'):
        rel = path.relative_to(Path(root) / 'web' / 'static')
        if {'vendor', 'uploads'}.intersection(rel.parts) or path.suffix not in ('.js', '.css'):
            continue
        text = path.read_text(encoding='utf-8-sig')
        path.write_text((jsmin if path.suffix == '.js' else cssmin)(text, keep_bang_comments=True), encoding='utf-8')


if __name__ == '__main__':
    minify('/app')
