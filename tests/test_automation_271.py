"""Isolated schedule, scope, cancellation and idempotency contracts; no live services."""
import contextlib
import ast
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from services.automation_schedule_service import normalize, next_times
from services.automation_scope_service import normalize_scope, predicate, matches

ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc


def schedule(**extra):
    return dict(mode='daily', times=['03:00'], days=[0, 4], start='2026-11-07', **extra)


def module(name):
    spec = importlib.util.spec_from_file_location('_test_' + name, ROOT / 'services' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class ScheduleTests(unittest.TestCase):
    def test_aden_midnight_is_previous_utc_day(self):
        s = schedule(); s['times'] = ['00:00']
        result = next_times(s, dt.datetime(2026, 11, 6, 20, tzinfo=UTC), 1)
        self.assertEqual(result, [dt.datetime(2026, 11, 6, 21, tzinfo=UTC)])

    def test_exclusive_after_and_multiple_times(self):
        s = schedule(); s['times'] = ['03:00', '12:00']
        self.assertEqual(next_times(s, dt.datetime(2026, 11, 7, tzinfo=UTC), 1)[0].hour, 9)

    def test_weekly_uses_sunday_zero(self):
        s = schedule(); s.update(mode='weekly', days=[0])
        self.assertEqual(next_times(s, dt.datetime(2026, 11, 7, tzinfo=UTC), 1)[0].day, 8)

    def test_month_last_day_and_skip(self):
        s = schedule(); s.update(mode='monthly', monthDay=31, start='2027-02-01')
        now = dt.datetime(2027, 2, 1, tzinfo=UTC)
        self.assertEqual(next_times(s, now, 1)[0].day, 28)
        s['fallback'] = 'skip'
        self.assertEqual(next_times(s, now, 1)[0].month, 3)

    def test_count_caps_actual_scheduled_attempts(self):
        s = schedule(); s.update(endMode='count', maxRuns=2)
        self.assertEqual(len(next_times(s, dt.datetime(2026, 11, 7, tzinfo=UTC), scheduled_count=1)), 1)
        self.assertEqual(next_times(s, scheduled_count=2), [])

    def test_once_does_not_replay_past_date(self):
        s = schedule(); s['mode'] = 'once'
        self.assertEqual(next_times(s, dt.datetime(2026, 11, 8, tzinfo=UTC)), [])

    def test_interval_is_anchored_not_completion_based(self):
        s = schedule(); s.update(mode='interval', interval=6, unit='hours', startTime='03:00')
        result = next_times(s, dt.datetime(2026, 11, 7, 7, tzinfo=UTC), 1)
        self.assertEqual(result[0], dt.datetime(2026, 11, 7, 12, tzinfo=UTC))

    def test_end_date_is_local_and_inclusive(self):
        s = schedule(); s.update(endMode='date', endDate='2026-11-07', times=['23:59'])
        self.assertEqual(len(next_times(s, dt.datetime(2026, 11, 7, tzinfo=UTC))), 1)

    def test_nonexistent_dst_wall_time_is_skipped(self):
        s = schedule(); s.update(start='2027-03-14', times=['02:30'], timezone='America/New_York')
        self.assertEqual(next_times(s, dt.datetime(2027, 3, 14, tzinfo=UTC), 1)[0].day, 15)

    def test_invalid_inputs_fail_instead_of_expanding_scope(self):
        for change in ({'times':['24:00']}, {'times':['03:00','03:00']}, {'mode':'weekly','days':[]},
                       {'monthDay':0}, {'unit':'seconds'}, {'endMode':'count','maxRuns':0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize(dict(schedule(), **change))


class ScopeTests(unittest.TestCase):
    def test_bound_parameters_cannot_change_sql(self):
        with self.assertRaises(ValueError):
            normalize_scope({'scope':'selected','package_ids':['1);DROP TABLE radacct']})
        clause, values = predicate({'scope':'selected','package_ids':[2,1,2]}, 'v')
        self.assertEqual((clause, values), ('v.package_id IN (?,?)', (1,2)))

    def test_scope_rechecked_after_account_changes(self):
        self.assertFalse(matches({'package_id':3}, {'scope':'selected','package_ids':[2]}))
        self.assertFalse(matches({'batch_id':4}, {'scope':'batch','batch_id':5}))
        self.assertTrue(matches({'batch_id':5}, {'scope':'batch','batch_id':5}))

    def test_empty_selection_cannot_become_all(self):
        with self.assertRaises(ValueError): normalize_scope({'scope':'selected','package_ids':[]})


class TaskContracts(unittest.TestCase):
    def setUp(self):
        database = types.ModuleType('database.db')
        for name in ('get_connection','query_all','query_one','execute_write','execute_update'):
            setattr(database, name, Mock(side_effect=AssertionError('Unexpected database access')))
        self.patch = patch.dict(sys.modules, {'database.db':database})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.tasks = module('automation_task_service')
        self.actions = module('automation_task_actions')

    def definition(self, kind='audit', **extra):
        return dict(name='fixture',kind=kind,schedule=normalize(schedule()),enabled=False,params={},**extra)

    def test_all_eight_kinds_validate_without_execution(self):
        for kind in self.tasks.KINDS:
            p = {'tables':['radacct']} if kind == 'optimize' else {}
            with self.subTest(kind=kind):
                self.assertEqual(self.tasks.validate(dict(self.definition(kind), params=p, consent=True))['kind'],kind)

    def test_string_false_is_not_destructive_consent(self):
        with self.assertRaises(ValueError): self.tasks.validate(self.definition('delete',consent='false'))

    def test_zero_age_explicitly_disables_deletion_age_filter(self):
        value=dict(self.definition('delete',consent=True),params={'age':0})
        self.assertEqual(self.tasks.validate(value)['params']['age'],0)

    def test_period_delivery_claim_prevents_second_send(self):
        bot=types.ModuleType('services.bot_notifications_service')
        bot.get_notification_settings=Mock(return_value={'settings':{'is_enabled':1,'bot_token':'fixture','chat_id':'fixture'}})
        bot.send_telegram_alert=Mock()
        for status,expected in [('sent','already_sent'),('sending_uncertain','needs_review')]:
            report={'success':True,'period_start':'2026-10-26','period_end':'2026-11-01','metrics':{}}
            with self.subTest(status=status),patch.dict(sys.modules,{'services.bot_notifications_service':bot}),patch.object(self.actions,'sales_summary',return_value=report),patch.object(self.actions,'execute_update',return_value=0),patch.object(self.actions,'query_one',return_value={'status':status}):
                result=self.actions.perform_action(self.definition('report'),lambda r:None,lambda:False,'id','run')
                self.assertEqual(result['telegram'],expected)
                self.assertEqual(result['partial'],status!='sent')
        bot.send_telegram_alert.assert_not_called()

    def test_arbitrary_table_and_backup_task_rejected(self):
        with self.assertRaises(ValueError): self.tasks.validate(self.definition('backup'))
        with self.assertRaises(ValueError): self.tasks.validate(dict(self.definition('optimize',consent=True),params={'tables':['wisp_license_runtime_state']}))

    def test_previous_complete_week_and_month(self):
        now = dt.datetime(2026,11,7)
        self.assertEqual(self.actions.sales_window('week',now),(dt.date(2026,10,26),dt.date(2026,11,1)))
        self.assertEqual(self.actions.sales_window('month',now),(dt.date(2026,10,1),dt.date(2026,10,31)))

    def test_temp_cleanup_preserves_unknown_files_and_backups(self):
        import os, time
        with tempfile.TemporaryDirectory() as root, patch.object(self.actions.tempfile,'gettempdir',return_value=root):
            good=Path(root,'max-pdf-abcdefgh');good.mkdir();file=good/'0.html';file.write_text('fixture')
            old=time.time()-100*86400;os.utime(file,(old,old));os.utime(good,(old,old))
            bad=Path(root,'max-pdf-ijklmnop');bad.mkdir();(bad/'backup.zip').write_bytes(b'protected');os.utime(bad,(old,old))
            result=self.actions.clean_temporary(7,lambda _:None,lambda:False)
            self.assertEqual(result['processed'],1);self.assertFalse(good.exists());self.assertTrue((bad/'backup.zip').exists())

    def test_temp_cancel_before_removal(self):
        with tempfile.TemporaryDirectory() as root, patch.object(self.actions.tempfile,'gettempdir',return_value=root):
            Path(root,'max-pdf-abcdefgh').mkdir()
            self.assertTrue(self.actions.clean_temporary(7,lambda _:None,lambda:True)['cancelled'])

    def test_report_stored_before_delivery_and_no_channel_is_success(self):
        bot=types.ModuleType('services.bot_notifications_service')
        bot.get_notification_settings=Mock(return_value={'settings':{}});bot.send_telegram_alert=Mock()
        with patch.dict(sys.modules,{'services.bot_notifications_service':bot}),patch.object(self.actions,'sales_summary',return_value={'success':True,'telegram':'not_configured'}) as report:
            progress=Mock();result=self.actions.perform_action(dict(self.definition('report'),params={},options={}),progress,lambda:False,'id','run')
            self.assertTrue(result['success']);progress.assert_called_once();bot.send_telegram_alert.assert_not_called()

    def test_report_send_failure_preserves_report_and_is_partial(self):
        bot=types.ModuleType('services.bot_notifications_service')
        bot.get_notification_settings=Mock(return_value={'settings':{'is_enabled':1,'bot_token':'fixture','chat_id':'fixture'}})
        bot.send_telegram_alert=Mock(return_value=(False,'uncertain'))
        report={'success':True,'period_start':'2026-10-26','period_end':'2026-11-01','metrics':{k:{'sold_count':1,'total_gross_revenue':'2.00'} for k in ('voucher','invoice')}}
        events=[]
        with patch.dict(sys.modules,{'services.bot_notifications_service':bot}),patch.object(self.actions,'sales_summary',return_value=report),patch.object(self.actions,'execute_update',return_value=1),patch.object(self.actions,'execute_write'):
            result=self.actions.perform_action(self.definition('report'),lambda r:events.append(json.loads(json.dumps(r))),lambda:False,'id','run')
        self.assertEqual(events[-1]['telegram'],'sending_uncertain')
        self.assertTrue(result['success']);self.assertTrue(result['partial']);bot.send_telegram_alert.assert_called_once()

    def test_cancel_and_completed_run_not_restarted(self):
        self.tasks.query_one=Mock(return_value={'state':'success'})
        lock=types.ModuleType('services.account_lifecycle_service')
        @contextlib.contextmanager
        def job_lock(*args):yield True
        lock.job_lock=job_lock
        with patch.object(self.tasks,'task_lock',job_lock),patch.object(self.tasks,'perform') as perform:
            self.tasks.worker('fixture')
            perform.assert_not_called()

    def test_periodic_report_sends_shared_html_layout_after_recording_result(self):
        bot=types.ModuleType('services.bot_notifications_service')
        bot.get_notification_settings=Mock(return_value={'settings':{'is_enabled':1,'bot_token':'fixture','chat_id':'fixture'}})
        events=[]
        bot.send_telegram_alert=Mock(side_effect=lambda *a,**kw:(events.append('sent') or (True,'fixture')))
        report={'success':True,'period_start':'2026-11-06','period_end':'2026-11-06',
            'metrics':{'voucher':{'sold_count':2,'total_gross_revenue':'5.50'},
                       'invoice':{'sold_count':1,'total_gross_revenue':'6.25'}},
            'activity':{'active_subscribers':180,'online_sessions':95}}
        with patch.dict(sys.modules,{'services.bot_notifications_service':bot}),patch.object(self.actions,'sales_summary',return_value=report),patch.object(self.actions,'execute_update',return_value=1),patch.object(self.actions,'execute_write'):
            value=dict(self.definition('report'),params={'period':'day'})
            self.actions.perform_action(value,lambda r:events.append('recorded'),lambda:False,'id','run')
        text=bot.send_telegram_alert.call_args.args[0]
        self.assertTrue(text.startswith('📊 <b>التقرير المالي والمبيعات اليومي - MAX RADIUS</b>'))
        self.assertIn('11.75 ريال',text)
        self.assertIn('95 جلسة',text)
        self.assertLess(events.index('recorded'),events.index('sent'))


class IdentityDisconnectContracts(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((ROOT/'services/nas_session_settlement.py').read_text(encoding='utf-8'))
        fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='disconnect_for_identity')
        ns={};exec(compile(ast.Module(body=[fn],type_ignores=[]),'identity-contract','exec'),ns)
        self.run=ns['disconnect_for_identity']
        self.disconnect=Mock(return_value={'success':True,'accounting_final':False})
        coa=types.ModuleType('services.coa_queue_service');coa._disconnect_single_session=self.disconnect
        settlement=types.ModuleType('services.renewal_settlement_service');settlement.SettlementPending=RuntimeError
        self.patch=patch.dict(sys.modules,{'services.coa_queue_service':coa,'services.renewal_settlement_service':settlement})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.snapshot={1:{'kind':'hotspot','row':dict(username='fixture',nasipaddress='192.0.2.1',acctsessionid='generation',framedipaddress='192.0.2.2',callingstationid='00:00:00:00:00:01',acctstarttime='fixture-start')}}

    def test_ack_or_absence_does_not_drop_captured_ids(self):
        self.run(self.snapshot)
        self.assertEqual(list(self.snapshot),[1])
        self.assertEqual(self.disconnect.call_args.kwargs,dict(lifecycle_stop=True,expected_start='fixture-start'))

    def test_failure_stops_before_next_session(self):
        self.disconnect.return_value={'success':False}
        self.snapshot[2]=self.snapshot[1]
        with self.assertRaises(RuntimeError):self.run(self.snapshot)
        self.disconnect.assert_called_once()

    def test_pppoe_uses_same_generation_scoped_path(self):
        self.snapshot[1]['kind']='pppoe'
        self.run(self.snapshot)
        self.assertEqual(self.disconnect.call_args.args[2],'generation')


if __name__ == '__main__': unittest.main()
