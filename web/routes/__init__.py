# -*- coding: utf-8 -*-
"""
MAX RADIUS Web Routes Package - Exports all 10 Functional Blueprints.
"""

from web.routes.auth import auth_bp
from web.routes.dashboard import dashboard_bp
from web.routes.vouchers import vouchers_bp
from web.routes.subscribers import subscribers_bp
from web.routes.packages import packages_bp
from web.routes.resellers import resellers_bp
from web.routes.nas import nas_bp
from web.routes.accounting import accounting_bp
from web.routes.system import system_bp
from web.routes.portal import portal_bp
from web.routes.whatsapp import whatsapp_bp

__all__ = [
    'auth_bp',
    'dashboard_bp',
    'vouchers_bp',
    'subscribers_bp',
    'packages_bp',
    'resellers_bp',
    'nas_bp',
    'accounting_bp',
    'system_bp',
    'portal_bp',
    'whatsapp_bp'
]
