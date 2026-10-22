"""Publisher identity: compiled and covered by the signed runtime manifest.

These values are release data, never tenant settings. No network lookup or
licensing decision is made here. Network names and support contacts stay separate.
"""
from types import MappingProxyType

PRODUCT_IDENTITY = MappingProxyType({
    'name': 'MAX RADIUS',
    'owner': 'MAX RADIUS',
    'phone': '967777366226',
    'whatsapp_url': 'https://wa.me/967777366226',
    'telephone_url': 'tel:+967777366226',
    'website': 'https://max-net.net',
})


def get_product_identity():
    """A fresh view prevents template/context mutations from changing identity."""
    return dict(PRODUCT_IDENTITY)
