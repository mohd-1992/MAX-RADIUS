"""Isolated publisher identity / template / signed-file checks; no app or DB."""
import base64
import importlib.util
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jinja2 import Environment, DictLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load('publisher_identity_fixture', 'core/product_identity.py')
integrity = load('runtime_integrity_fixture', 'core/runtime_integrity.py')
embedding = load('template_embedding_fixture', 'packaging/embed_templates.py')


class ProductProtectionTests(unittest.TestCase):
    def test_identity_has_one_approved_destination(self):
        value = identity.get_product_identity()
        self.assertEqual(value['phone'], '967777366226')
        self.assertEqual(value['whatsapp_url'], 'https://wa.me/' + value['phone'])
        self.assertEqual(value['telephone_url'], 'tel:+' + value['phone'])

    def test_tenant_changes_cannot_mutate_release_identity(self):
        value = identity.get_product_identity()
        value['name'] = 'tenant override'
        self.assertEqual(identity.get_product_identity()['name'], 'MAX RADIUS')
        with self.assertRaises(TypeError):
            identity.PRODUCT_IDENTITY['phone'] = 'other'

    def test_app_name_not_owned_by_environment(self):
        source = (ROOT / 'core/config.py').read_text(encoding='utf-8-sig')
        self.assertIn("APP_NAME = PRODUCT_IDENTITY['name']", source)
        self.assertNotIn("os.environ.get('APP_NAME'", source)

    def test_loader_is_release_owned(self):
        source = (ROOT / 'web/__init__.py').read_text(encoding='utf-8-sig')
        self.assertIn('app.jinja_loader = template_loader()', source)
        self.assertNotIn('ChoiceLoader', source)

    def test_embed_preserves_all_templates_and_escaping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'web/templates/nested'
            target.mkdir(parents=True)
            (target / 'page.html').write_text('اسم {{ name }}', encoding='utf-8')
            original = embedding.embed(root)
            spec = importlib.util.spec_from_file_location('embedded_fixture', root / 'web/_embedded_templates.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            (target / 'page.html').write_text('tampered', encoding='utf-8')
            env = Environment(loader=module.template_loader(), autoescape=select_autoescape())
            self.assertEqual(env.get_template('nested/page.html').render(name='<script>'), 'اسم &lt;script&gt;')
            self.assertEqual(original['nested/page.html'], 'اسم {{ name }}')

    def test_embedding_empty_input_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                embedding.embed(folder)

    def render_license(self, state='verified'):
        text = (ROOT / 'web/templates/license_status.html').read_text(encoding='utf-8-sig')
        env = Environment(loader=DictLoader({'license_status.html': text,
            'base.html': '{% block content %}{% endblock %}'}), autoescape=select_autoescape())
        return env.get_template('license_status.html').render(
            license={'valid': False, 'status': 'expired'},
            product_identity=identity.get_product_identity(), runtime_integrity={'state': state},
            current_year=2026, app_version='fixture', url_for=lambda *a, **k: '/fixture', csrf_token=lambda: 'fixture')

    def test_license_view_displays_contact_without_automatic_send(self):
        html = self.render_license()
        self.assertIn('https://wa.me/967777366226', html)
        self.assertIn('tel:+967777366226', html)
        self.assertNotIn('wa.me/967777123456', html)
        self.assertNotIn('wa.me/967777000000', html)
        self.assertNotIn('wa.me/967777366226?text=', html)

    def test_integrity_warning_is_not_license_enforcement(self):
        html = self.render_license('modified')
        self.assertIn('لا يوقف المصادقة أو المحاسبة', html)
        self.assertIn('https://wa.me/967777366226', html)

    def test_owner_support_stays_separate(self):
        for relative in ('web/templates/user_portal/login.html', 'web/templates/user_portal/dashboard.html'):
            source = (ROOT / relative).read_text(encoding='utf-8-sig')
            self.assertIn('portal_whatsapp', source)
            self.assertNotIn('product_identity.whatsapp_url', source)

    def test_signed_runtime_cases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'app'
            control = Path(folder) / 'control'
            root.mkdir(); control.mkdir()
            file = root / 'module.so'
            file.write_bytes(b'release fixture')
            key = Ed25519PrivateKey.generate()
            data = json.dumps({'format': 1, 'files': {'module.so': hashlib.sha256(file.read_bytes()).hexdigest()}}).encode()
            (control / 'runtime-manifest.json').write_bytes(data)
            (control / 'runtime-manifest.sig').write_bytes(base64.b64encode(key.sign(data)))
            (control / 'runtime-public.pem').write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'verified')
            file.write_bytes(b'changed')
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'modified')
            file.write_bytes(b'release fixture')
            extra = root / 'injected.py'; extra.write_text('pass')
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'modified')
            extra.unlink()
            storage = root / 'storage'; storage.mkdir(); (storage / 'data').write_bytes(b'tenant')
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'verified')
            file.unlink()
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'modified')
            (control / 'runtime-manifest.sig').write_bytes(b'invalid')
            self.assertEqual(integrity.check_runtime(root, control)['state'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
