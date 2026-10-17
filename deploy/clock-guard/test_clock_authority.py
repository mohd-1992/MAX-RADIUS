"""No app imports, sockets, database or host-clock mutation."""
import ast
import datetime as dt
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('isolated_time_service', ROOT/'core/time_service.py')
        self.clock = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.clock)
        self.clock._CACHED_TIMEZONE_NAME = 'Asia/Aden'

    def probe(self, offset):
        client = types.SimpleNamespace(request=lambda *a, **kw: types.SimpleNamespace(offset=offset))
        self.clock.ntplib = types.SimpleNamespace(NTPClient=lambda: client)
        self.clock.NTPLIB_AVAILABLE = True
        return self.clock.sync_ntp_time()

    def test_01_cached_positive_and_negative_offsets_never_change_epoch(self):
        with patch.object(self.clock.time, 'time', return_value=1800000000):
            for offset in (-10743, 10743, .2):
                self.clock._NTP_OFFSET_SECONDS = offset
                self.assertEqual(self.clock.get_real_timestamp(), 1800000000)

    def test_02_large_probe_is_diagnostic_disagreement(self):
        self.assertFalse(self.probe(10743)[0])
        self.assertFalse(self.clock.get_time_sync_status()['offset_applied'])

    def test_03_small_probe_reports_agreement(self):
        self.assertTrue(self.probe(.1)[0])
        self.assertTrue(self.clock.get_time_sync_status()['ntp_synced'])

    def test_04_nonfinite_probes_fail(self):
        for offset in (float('nan'), float('inf'), -float('inf')):
            self.assertFalse(self.probe(offset)[0])

    def test_05_failed_probe_clears_previous_agreement(self):
        self.probe(.1)
        self.clock.NTPLIB_AVAILABLE = False
        self.assertFalse(self.clock.sync_ntp_time()[0])
        self.assertFalse(self.clock.get_time_sync_status()['ntp_synced'])

    def test_06_stale_probe_does_not_claim_fresh_sync(self):
        self.probe(.1)
        self.clock._LAST_NTP_PROBE_MONOTONIC -= 7201
        self.assertFalse(self.clock.get_time_sync_status()['ntp_synced'])

    def test_07_zones_preserve_epoch_db_cutoff_and_expiry(self):
        with patch.object(self.clock.time, 'time', return_value=1800000000):
            baseline = self.clock.get_db_cutoff_str()
            for zone in ('UTC','Asia/Aden','Asia/Dubai','Asia/Kathmandu','America/New_York',
                         'Africa/Cairo','Australia/Lord_Howe','Pacific/Kiritimati','Pacific/Pago_Pago'):
                self.clock._CACHED_TIMEZONE_NAME = zone
                self.assertEqual(self.clock.get_system_now().timestamp(), 1800000000)
                self.assertEqual(self.clock.get_db_cutoff_str(), baseline)
                self.assertEqual(self.clock.get_db_storage_now().utcoffset(), dt.timedelta(hours=3))
                self.assertEqual((dt.datetime.fromtimestamp(1800000060,dt.timezone.utc)-self.clock.get_system_now()).total_seconds(),60)

    def test_08_dst_forward_preserves_elapsed_seconds(self):
        before=dt.datetime(2026,3,8,6,59,59,tzinfo=dt.timezone.utc)
        after=before+dt.timedelta(seconds=2)
        a=self.clock.to_system_timezone(before,'America/New_York')
        b=self.clock.to_system_timezone(after,'America/New_York')
        self.assertEqual((a.hour,b.hour),(1,3))
        self.assertEqual(b.timestamp()-a.timestamp(),2)

    def test_09_dst_repeated_hour_has_distinct_epoch(self):
        a=dt.datetime(2026,11,1,5,30,tzinfo=dt.timezone.utc)
        b=a+dt.timedelta(hours=1)
        x=self.clock.to_system_timezone(a,'America/New_York')
        y=self.clock.to_system_timezone(b,'America/New_York')
        self.assertEqual((x.hour,y.hour),(1,1))
        self.assertEqual(y.timestamp()-x.timestamp(),3600)

    def test_10_naive_db_dates_still_mean_fixed_plus_three(self):
        value=dt.datetime(2026,10,17,3)
        self.assertEqual(self.clock.to_system_timezone(value,'UTC').hour,0)

    def test_11_status_uses_one_epoch_for_all_formats(self):
        with patch.object(self.clock.time,'time',return_value=1800000000):
            status=self.clock.get_time_sync_status()
            self.assertEqual(status['timestamp_ms'],1800000000000)
            self.assertEqual(status['clock_source'],'host')
            self.assertEqual(status['ntp_role'],'diagnostic')
            self.assertEqual(status['utc_now'],dt.datetime.fromtimestamp(1800000000,dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))

    def test_12_fallback_cutoff_ignores_os_local_zone(self):
        with patch.object(self.clock,'get_db_storage_now',side_effect=ValueError), patch.object(self.clock.time,'time',return_value=0):
            self.assertEqual(self.clock.get_db_cutoff_str(), '1970-01-01 02:55:00')

    def test_13_timezone_setting_does_not_change_host_or_db_contract(self):
        fake=types.ModuleType('database.db');calls=[]
        fake.set_active_db_timezone=lambda name:calls.append(name)
        with patch.dict('sys.modules',{'database.db':fake}), patch.dict('os.environ',{},clear=False):
            import os
            previous=os.environ.get('TZ')
            self.clock.update_system_timezone('America/New_York')
            self.assertEqual(os.environ.get('TZ'),previous)
            self.assertEqual(self.clock.get_db_storage_timezone().utcoffset(None),dt.timedelta(hours=3))
        tree=ast.parse((ROOT/'database/db.py').read_text(encoding='utf-8-sig'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='set_active_db_timezone')
        self.assertFalse(any(isinstance(n,ast.Call) for n in ast.walk(fn)))

    def test_14_diagnostic_route_does_not_reschedule_backups(self):
        tree=ast.parse((ROOT/'web/routes/system.py').read_text(encoding='utf-8-sig'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='api_time_sync')
        fn.decorator_list=[]
        env={'sync_ntp_time':lambda **kw:(True,0,'fixture','ok'),
             'get_time_sync_status':lambda:{'clock_source':'host'},'jsonify':lambda data:data,
             'reload_backup_schedule':lambda:self.fail('Diagnostic probe changed scheduler')}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'isolated_route','exec'),env)
        self.assertTrue(env['api_time_sync']()['success'])

    def test_15_settings_template_parses_without_app_start(self):
        from jinja2 import Environment
        Environment().parse((ROOT/'web/templates/settings.html').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main(verbosity=2)
