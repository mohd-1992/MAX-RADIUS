"""Source/compiled service checks with synthetic data and no application startup."""
import importlib.util
import ast
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from werkzeug.datastructures import FileStorage

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get('MAX_COMPILED_SETTINGS_FIXTURE') == 'synthetic-owned':
    ROOT = Path('/fixture')


def load(module, path):
    specification = importlib.util.spec_from_file_location(module, path)
    value = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(value)
    return value


if os.environ.get('MAX_COMPILED_SETTINGS_FIXTURE') == 'synthetic-owned':
    from services import system_settings_service as settings
    from services import migration_upload_service as migration
    from core import license_feature_policy as policy
else:
    settings = load('settings_fixture', ROOT/'services/system_settings_service.py')
    migration = load('migration_fixture', ROOT/'services/migration_upload_service.py')
    from core import license_feature_policy as policy


class SettingsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='max-settings-test-')
        self.root = Path(self.temporary.name)
        self.db = self.root/'fixture.sqlite'
        with self.transaction() as connection:
            connection.executescript('''CREATE TABLE wisp_system_settings (`key` TEXT PRIMARY KEY, `value` TEXT, description TEXT);
                CREATE TABLE wisp_audit_logs(id INTEGER PRIMARY KEY,admin_id INTEGER,username TEXT,action TEXT,module TEXT,details TEXT,ip_address TEXT);
                INSERT INTO wisp_system_settings VALUES ('network_name','original',''),('network_logo','old.png',''),('currency','YER','');''')
        self.static = self.root/'static'
        (self.static/'uploads').mkdir(parents=True)
        (self.static/'uploads'/'old.png').write_bytes(b'original-logo')
        self.database = patch.object(settings, '_database', return_value=(self.connect, lambda sql, conn:sql))
        self.database.start()
        self.license_patch = patch.object(policy, 'verified_state', return_value={'valid': True, 'features': {'white_label': True}})
        self.license = self.license_patch.start()

    def tearDown(self):
        self.license_patch.stop()
        self.database.stop()
        self.temporary.cleanup()

    def connect(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def values(self):
        with self.transaction() as connection:
            return dict(connection.execute('SELECT `key`,`value` FROM wisp_system_settings'))

    def logo(self, filename='brand.png'):
        return FileStorage(stream=io.BytesIO(b'synthetic-logo'), filename=filename)

    def save(self, form=None, logo=None):
        return settings.save_system_settings(form or {}, logo, self.static)

    def test_name_and_aliases_saved_together(self):
        self.save({'network_name':'synthetic-network'})
        self.assertTrue(all(self.values()[k]=='synthetic-network' for k in ('network_name','company_name','isp_name')))

    def test_logo_and_settings_commit_together_and_keep_old_file(self):
        self.save({'network_name':'synthetic-network'}, self.logo())
        self.assertEqual((self.static/'uploads'/self.values()['network_logo']).read_bytes(), b'synthetic-logo')
        self.assertEqual((self.static/'uploads'/'old.png').read_bytes(), b'original-logo')

    def test_disabled_branding_blocks_direct_post_before_effects(self):
        self.license.return_value={'valid':True,'features':{'white_label':False}}
        before=self.values()
        with self.assertRaises(policy.FeatureUnavailable):self.save({'network_name':'forbidden'}, self.logo())
        self.assertEqual(self.values(), before)
        self.assertEqual([p.name for p in (self.static/'uploads').iterdir()], ['old.png'])

    def test_disabled_branding_allows_general_settings_and_preserves_identity(self):
        self.license.return_value={'valid':True,'features':{'white_label':False}}
        self.save({'currency':'USD','timezone':'UTC'})
        self.assertEqual(self.values()['network_name'],'original')
        self.assertEqual(self.values()['network_logo'],'old.png')
        self.assertEqual(self.values()['currency'],'USD')

    def test_unverified_license_cannot_grant_branding(self):
        self.license.return_value={'valid':False,'features':{'white_label':True}}
        with self.assertRaises(policy.FeatureUnavailable):self.save({'network_name':'forbidden'})

    def test_legacy_valid_license_default_is_preserved(self):
        self.license.return_value={'valid':True,'features':{}}
        self.save({'network_name':'legacy'})
        self.assertEqual(self.values()['network_name'],'legacy')

    def test_invalid_logo_has_no_database_or_file_effect(self):
        before=self.values()
        with self.assertRaises(ValueError):self.save({'network_name':'new'}, self.logo('file.exe'))
        self.assertEqual(self.values(), before)

    def test_late_database_failure_rolls_back_name_logo_and_audit(self):
        with self.transaction() as connection:
            connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON wisp_audit_logs BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before=self.values()
        with self.assertRaises(sqlite3.IntegrityError):self.save({'network_name':'new'}, self.logo())
        self.assertEqual(self.values(), before)
        self.assertEqual([p.name for p in (self.static/'uploads').iterdir()], ['old.png'])

    def test_file_failure_leaves_database_unchanged(self):
        before=self.values()
        with patch.object(settings.os,'replace',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):self.save({'network_name':'new'}, self.logo())
        self.assertEqual(self.values(), before)
        self.assertEqual([p.name for p in (self.static/'uploads').iterdir()], ['old.png'])

    def test_commit_failure_is_unknown_and_preserves_possible_logo(self):
        original=self.connect
        class Connection:
            def __init__(self):self.real=original()
            def cursor(self):return Cursor(self.real.cursor())
            def commit(self):self.real.commit();raise OSError('synthetic lost reply')
            def rollback(self):self.real.rollback()
            def close(self):self.real.close()
        class Cursor:
            def __init__(self, real):self.real=real
            def execute(self, sql, params=()):return self.real.execute(sql.replace(' FOR UPDATE',''),params)
            def fetchall(self):return self.real.fetchall()
            def close(self):self.real.close()
        with patch.object(settings, '_database', return_value=(Connection,lambda sql, conn:sql)):
            with self.assertRaises(settings.SettingsSaveUncertain):self.save({'network_name':'committed'}, self.logo())
        self.assertEqual(self.values()['network_name'],'committed')
        self.assertTrue((self.static/'uploads'/self.values()['network_logo']).is_file())

    def test_currency_timezone_and_portal_values(self):
        self.save({'currency':'USD','timezone':'UTC','portal_allow_registration':'1','loan_amount_mb':'2048'})
        saved=self.values()
        self.assertEqual((saved['currency_symbol'],saved['timezone'],saved['portal_allow_registration'],saved['loan_amount_mb']),('$','UTC','1','2048'))

    def test_audit_is_in_same_transaction(self):
        self.save({'network_name':'audited'})
        with self.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM wisp_audit_logs WHERE action='UPDATE_SETTINGS'").fetchone()[0],1)

    def test_migration_upload_uses_tenant_storage(self):
        directory=self.root/'storage'/'migration_cache'
        directory.mkdir(parents=True)
        with patch.object(migration, 'cache_directory', return_value=directory):
            path=Path(migration.save_backup_upload(self.logo('uploaded.gz')))
            self.assertTrue(path.is_relative_to(self.root/'storage'))
            self.assertEqual(migration.cached_backup(),str(path))

    def test_failed_upload_preserves_previous_cached_backup(self):
        directory=self.root/'storage'/'migration_cache';directory.mkdir(parents=True)
        target=directory/'uploaded_backup.gz';target.write_bytes(b'previous')
        upload=self.logo('uploaded.gz')
        with patch.object(migration,'cache_directory',return_value=directory),patch.object(upload,'save',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):migration.save_backup_upload(upload)
        self.assertEqual(target.read_bytes(),b'previous')
        self.assertEqual(list(directory.iterdir()),[target])

    def test_disabled_identity_is_hidden_in_shared_context(self):
        source=(ROOT/'web/routes/system.py').read_text(encoding='utf-8-sig')
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='verified_branding_context')
        node.decorator_list=[]
        namespace={'APP_NAME':'MAX RADIUS'}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'branding-context','exec'),namespace)
        self.license.return_value={'valid':True,'features':{'white_label':False}}
        result=namespace['verified_branding_context']()
        self.assertEqual(result['network_name'],'MAX RADIUS')
        self.assertIsNone(result['logo_url'])
        self.license.return_value={'valid':True,'features':{'white_label':True}}
        self.assertEqual(namespace['verified_branding_context'](),{})


if __name__=='__main__':unittest.main()
