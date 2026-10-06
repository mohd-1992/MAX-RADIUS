"""Persistent installation secrets and CSRF protection for license administration."""
import os
import secrets
import hmac
import re
import time
from pathlib import Path


def persistent_secret(storage, env_name='SECRET_KEY'):
    supplied = os.environ.get(env_name)
    if supplied:
        if len(supplied) < 32 or supplied.startswith(('max-radius-secret', 'max-license-master-secret')):
            raise RuntimeError('Configure a unique session secret of at least 32 characters')
        return supplied
    path = Path(storage) / 'secrets' / 'session.key'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32))
    for _ in range(20):
        value = path.read_text().strip()
        if len(value) >= 32:
            return value
        time.sleep(0.01)
    raise RuntimeError('Installation session secret is incomplete')


def install_csrf(app, protected):
    from flask import request, session, abort
    def token():
        if '_license_csrf' not in session:
            session['_license_csrf'] = secrets.token_urlsafe(32)
        return session['_license_csrf']
    app.jinja_env.globals['csrf_token'] = token
    @app.before_request
    def check_csrf():
        if request.method == 'POST' and protected(request):
            expected = session.get('_license_csrf', '')
            actual = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            if not expected or not isinstance(actual, str) or not hmac.compare_digest(expected, actual):
                abort(403, 'Invalid CSRF token')
    @app.after_request
    def inject_form_tokens(response):
        if response.mimetype == 'text/html' and not response.direct_passthrough:
            value = token()
            html = response.get_data(as_text=True)
            html = re.sub(r'(<form\b[^>]*\bmethod\s*=\s*[\"\x27]post[\"\x27][^>]*>)',
                          lambda m: m.group(1) + '<input type="hidden" name="csrf_token" value="' + value + '">',
                          html, flags=re.I)
            response.set_data(html)
        return response
