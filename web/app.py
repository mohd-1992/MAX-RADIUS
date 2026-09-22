# -*- coding: utf-8 -*-
"""
MAX RADIUS Web Application Entrypoint.
Exposes the central Flask WSGI application instance `app` created via `create_app()`.
"""

from web import create_app

app = create_app()

if __name__ == '__main__':
    from core.config import APP_HOST, APP_PORT, DEBUG
    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG)
