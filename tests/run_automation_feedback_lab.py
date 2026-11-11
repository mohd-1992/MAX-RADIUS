"""Explicit disposable MariaDB acceptance; never imports the application.

Run with --run, DB_NAME=automation_feedback_fixture and a dedicated DB container.
Only synthetic automation definitions/results are created or removed.
"""
import contextlib
import datetime as dt
import importlib
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


def main():
    if sys.argv[1:] not in (['--run'], ['--run', '--compiled']) or os.environ.get('DB_NAME') != 'automation_feedback_fixture':
        raise SystemExit('Requires --run and the dedicated automation_feedback_fixture database')
    import pymysql
    sys.path.insert(0, '/app' if '--compiled' in sys.argv else str(Path(__file__).resolve().parents[1]))
    db = types.ModuleType('database.db')
    def connection():
        return pymysql.connect(host=os.environ['DB_HOST'], user='root',
                               password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'],
                               charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
    def query(sql, args=(), mode='all'):
        conn = connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql.replace('?', '%s'), args)
                result = cur.fetchone() if mode == 'one' else cur.fetchall() if mode == 'all' else cur.rowcount
            conn.commit()
            return result
        finally:
            conn.close()
    db.get_connection = connection
    db.is_mysql_conn = lambda c: True
    db.adapt_query = lambda sql, c: sql.replace('?', '%s')
    db.query_all = lambda sql, args=(): query(sql, args)
    db.query_one = lambda sql, args=(): query(sql, args, 'one')
    db.execute_write = db.execute_update = lambda sql, args=(): query(sql, args, 'count')
    clock = types.ModuleType('core.time_service')
    clock.get_configured_timezone_name = lambda: 'Asia/Aden'
    guard = types.ModuleType('core.license_feature_policy')
    guard.guarded = lambda name: lambda fn: fn
    guard.enabled = lambda name: True
    sys.modules.update({'database.db': db, 'core.time_service': clock, 'core.license_feature_policy': guard})
    from services import automation_task_service as tasks, automation_defaults_service as defaults
    from services import automation_rules_service as legacy, automation_task_actions as actions
    if '--compiled' in sys.argv:
        assert all(str(m.__file__).endswith('.so') for m in (tasks, defaults, legacy, actions))

    class Acceptance(unittest.TestCase):
        def setUp(self):
            conn = connection()
            with conn.cursor() as cur:
                cur.execute('SHOW TABLES')
                for row in cur.fetchall():
                    name = next(iter(row.values()))
                    assert name.startswith('wisp_automation_')
                    cur.execute('DROP TABLE `' + name + '`')
            conn.close()
            tasks._ready = False
            legacy._READY = False
            tasks.ensure_tables(seed_defaults=False)

        def definitions(self):
            return defaults.default_definitions(dt.datetime.now(dt.timezone.utc))

        def install(self):
            self.assertTrue(defaults.install_defaults())

        def rows(self):
            return db.query_all('SELECT * FROM wisp_automation_tasks ORDER BY id')

        def insert(self, key='delete-expired', ident=None, **changes):
            item = next(d for d in self.definitions() if d['key'] == key)
            value = tasks.validate(dict(item['value'], **changes))
            ident = ident or item['id']
            db.execute_write('INSERT INTO wisp_automation_tasks(id,name,kind,enabled,definition,created_at) VALUES(?,?,?,?,?,UTC_TIMESTAMP())',
                             (ident, value['name'], value['kind'], value['enabled'], tasks.dump(value)))
            return ident

        def test_fresh_defaults_enabled_and_repeat_install_unchanged(self):
            self.install()
            before = self.rows()
            self.assertEqual(len(before), 6)
            self.assertTrue(all(r['enabled'] and r['next_at'] for r in before))
            self.install()
            self.assertEqual(self.rows(), before)

        def test_different_custom_age_does_not_disable_default(self):
            self.insert(ident='a'*32, params=dict(age=1, batch=5000, reason='all'))
            self.install()
            default = db.query_one('SELECT enabled FROM wisp_automation_tasks WHERE id=?', (self.definitions()[0]['id'],))
            self.assertEqual(default['enabled'], 1)
            self.assertEqual(len(self.rows()), 7)

        def test_equivalent_custom_action_reused_without_second_schedule(self):
            self.insert(ident='a'*32)
            self.install()
            self.assertIsNone(db.query_one('SELECT id FROM wisp_automation_tasks WHERE id=?', (self.definitions()[0]['id'],)))
            self.assertEqual(len(self.rows()), 6)
            self.install()
            self.assertEqual(len(self.rows()), 6)

        def test_scope_and_reason_are_not_kind_only_conflicts(self):
            self.insert(ident='a'*32, params=dict(age=14, batch=5000, reason='time', scope='batch', batch_id=4))
            self.install()
            self.assertEqual(len(self.rows()), 7)

        def test_existing_age_one_and_disabled_choice_preserved(self):
            ident = self.insert(enabled=False, params=dict(age=1, batch=5000, reason='all'))
            before = db.query_one('SELECT * FROM wisp_automation_tasks WHERE id=?', (ident,))
            self.install()
            self.assertEqual(db.query_one('SELECT * FROM wisp_automation_tasks WHERE id=?', (ident,)), before)

        def test_permanent_removal_retains_history_and_never_reseeds(self):
            self.install()
            ident = self.definitions()[0]['id']
            value = db.query_one('SELECT definition FROM wisp_automation_tasks WHERE id=?', (ident,))['definition']
            db.execute_write("INSERT INTO wisp_automation_runs(id,task_id,slot_key,state,definition,result,created_at,updated_at) VALUES(?,?,?,'success',?,'{}',UTC_TIMESTAMP(),UTC_TIMESTAMP())", ('b'*32, ident, ident+':fixture', value))
            tasks.remove(ident, 1)
            self.assertIsNone(db.query_one('SELECT * FROM wisp_automation_tasks WHERE id=?', (ident,)))
            self.assertEqual(len(db.query_all('SELECT * FROM wisp_automation_runs')), 1)
            self.install()
            self.assertEqual(len(self.rows()), 5)

        def test_wrong_revision_and_active_run_reject_removal(self):
            ident = self.insert()
            with self.assertRaises(ValueError): tasks.remove(ident, 2)
            value = self.rows()[0]['definition']
            db.execute_write("INSERT INTO wisp_automation_runs(id,task_id,slot_key,state,definition,result,created_at,updated_at) VALUES(?,?,?,'queued',?,'{}',UTC_TIMESTAMP(),UTC_TIMESTAMP())", ('b'*32, ident, ident+':fixture', value))
            with self.assertRaises(ValueError): tasks.remove(ident, 1)
            self.assertEqual(len(self.rows()), 1)

        def test_config_lock_blocks_concurrent_delete(self):
            ident = self.insert()
            with tasks.task_lock('config') as acquired:
                self.assertTrue(acquired)
                with self.assertRaises(ValueError): tasks.remove(ident, 1)
            self.assertEqual(len(self.rows()), 1)

        def test_old_soft_deleted_default_purged_and_receipted(self):
            ident = self.insert()
            db.execute_write('UPDATE wisp_automation_tasks SET deleted_at=UTC_TIMESTAMP() WHERE id=?', (ident,))
            self.install()
            self.assertEqual(len(self.rows()), 5)
            self.install()
            self.assertEqual(len(self.rows()), 5)

        def test_legacy_retired_without_reseeding_or_erasing_log(self):
            legacy.ensure_automation_tables()
            self.assertEqual(db.query_all('SELECT * FROM wisp_automation_rules'), ())
            db.execute_write("INSERT INTO wisp_automation_rules(rule_name,trigger_event,action_type,is_active,schedule_enabled) VALUES('fixture','DAILY_MIDNIGHT','CLEAN_EXPIRED_CARDS',1,1)")
            db.execute_write("INSERT INTO wisp_automation_logs(rule_id,rule_name,details) VALUES(1,'fixture','recorded')")
            self.install()  # also after VERSION=2: older-version rollback may have reseeded
            self.assertEqual(db.query_all('SELECT * FROM wisp_automation_rules'), ())
            self.assertEqual(len(db.query_all('SELECT * FROM wisp_automation_logs')), 1)
            legacy._READY = False
            legacy.ensure_automation_tables()
            self.assertEqual(db.query_all('SELECT * FROM wisp_automation_rules'), ())
            self.assertFalse(legacy.toggle_rule(1, True)[0])
            self.assertFalse(legacy.execute_rule_now(1)[0])

        def test_legacy_worker_lock_defers_retirement_atomically(self):
            legacy.ensure_automation_tables()
            db.execute_write("INSERT INTO wisp_automation_rules(rule_name,trigger_event,action_type) VALUES('fixture','DAILY_MIDNIGHT','CLEAN_EXPIRED_CARDS')")
            from services.account_lifecycle_service import job_lock
            with job_lock('automation-rule-1') as acquired:
                self.assertTrue(acquired)
                with self.assertRaises(ValueError): defaults.install_defaults()
            self.assertEqual(len(db.query_all('SELECT * FROM wisp_automation_rules')), 1)

        def test_review_closes_when_enabled_or_conflict_gone(self):
            ident = self.insert(enabled=False)
            self.insert(ident='a'*32)
            db.execute_write("INSERT INTO wisp_automation_notices VALUES(?,'review','fixture',UTC_TIMESTAMP())", (ident,))
            self.assertEqual(len(tasks.task_notices()), 1)
            self.assertEqual(tasks.task_notices(disk=True), [])
            db.execute_write('DELETE FROM wisp_automation_tasks WHERE id=?', ('a'*32,))
            self.assertEqual(tasks.task_notices(), [])
            db.execute_write("INSERT INTO wisp_automation_notices VALUES(?,'review','fixture',UTC_TIMESTAMP())", (ident,))
            db.execute_write('UPDATE wisp_automation_tasks SET enabled=1 WHERE id=?', (ident,))
            self.assertEqual(tasks.task_notices(), [])

        def test_disk_healthy_silent_recovery_recorded_once(self):
            value = tasks.validate(dict(self.definitions()[1]['value'], kind='alert', params={'threshold':15}))
            config = types.ModuleType('core.config'); config.STORAGE_DIR = '/tmp'
            ident = 'a'*32
            def check(free):
                with patch.dict(sys.modules, {'core.config':config}), patch.object(actions.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=free, total=100)):
                    return actions.perform_action(value, lambda _: None, lambda: False, ident, 'run')
            self.assertFalse(check(50)['changed'])
            self.assertEqual(db.query_all('SELECT * FROM wisp_automation_notices'), ())
            self.assertTrue(check(5)['changed'])
            self.assertFalse(check(4)['changed'])
            self.assertTrue(check(50)['changed'])
            before = db.query_all('SELECT * FROM wisp_automation_notices')
            self.assertFalse(check(60)['changed'])
            self.assertEqual(db.query_all('SELECT * FROM wisp_automation_notices'), before)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == '__main__':
    main()
