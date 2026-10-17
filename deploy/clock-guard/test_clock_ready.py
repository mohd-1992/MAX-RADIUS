"""Isolated tests. No customer, Docker daemon, DBus, NTP, or real clock writes."""
import importlib.util
import io
import contextlib
import math
from pathlib import Path
import stat
import subprocess
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('clock_ready_fixture', ROOT / 'clock_ready.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.calls = 0
        self.outputs = []

    def sleep(self, seconds):
        self.now += seconds

    def run(self, values, wait=10, duration=0):
        seq = iter(values)
        last = False
        def probe(timeout):
            nonlocal last
            self.calls += 1
            self.now += min(timeout, duration)
            last = next(seq, last)
            return last
        return gate.wait_ready(wait, 1, 1, monotonic=lambda: self.now,
                               sleep=self.sleep, probe=probe, emit=self.outputs.append,
                               wall_clock=lambda:self.now)


class ReadinessTests(unittest.TestCase):
    def moving_clock(self, walls, wait=5):
        clock=Clock();values=iter(walls);last=walls[-1]
        def wall():return next(values,last)
        ok=gate.wait_ready(wait,1,1,monotonic=lambda:clock.now,sleep=clock.sleep,
            probe=lambda _:True,wall_clock=wall,emit=clock.outputs.append)
        return ok,clock

    def test_sync_flags_do_not_hide_backward_step(self):
        ok,c=self.moving_clock([10000,1,2,3])
        self.assertTrue(ok);self.assertEqual(c.now,3)
        self.assertTrue(any('step observed' in line for line in c.outputs))

    def test_sync_flags_do_not_hide_forward_step(self):
        ok,c=self.moving_clock([1,10000,10001,10002])
        self.assertTrue(ok);self.assertEqual(c.now,3)

    def test_continual_steps_never_permit_start(self):
        ok,c=self.moving_clock([0,100,0,100,0,100],wait=5)
        self.assertFalse(ok)

    def test_small_slew_is_allowed(self):
        ok,c=self.moving_clock([100,101.01]);self.assertTrue(ok);self.assertEqual(c.now,1)

    def test_nonfinite_clock_never_permits_start(self):
        for bad in (math.inf,math.nan):
            ok,c=self.moving_clock([bad],wait=3);self.assertFalse(ok)

    def test_two_confirmations_are_required(self):
        c = Clock(); self.assertTrue(c.run([True, True])); self.assertEqual(c.calls, 2)

    def test_no_sync_defers_start(self):
        c = Clock(); self.assertFalse(c.run([False])); self.assertLessEqual(c.now, 10)

    def test_late_sync_recovers_without_changes(self):
        c = Clock(); self.assertTrue(c.run([False, False, True, True]))

    def test_transient_sync_does_not_pass(self):
        c = Clock(); self.assertFalse(c.run([True, False]))

    def test_transient_failure_resets_confirmations(self):
        c = Clock(); self.assertTrue(c.run([True, False, True, True])); self.assertEqual(c.calls, 4)

    def test_success_after_deadline_is_rejected(self):
        c = Clock(); self.assertFalse(c.run([True, True], wait=2, duration=1))

    def test_wall_clock_rollback_cannot_extend_deadline(self):
        c = Clock()
        with patch('time.time', side_effect=[100000, 1, 300000]):
            self.assertFalse(c.run([False]))
        self.assertEqual(c.now, 10)

    def test_wall_clock_jump_cannot_authorize_without_sync(self):
        c = Clock()
        with patch('time.time', return_value=999999999999):
            self.assertFalse(c.run([False]))

    def test_last_moment_single_confirmation_does_not_pass(self):
        c = Clock(); self.assertFalse(c.run([False] * 9 + [True]))

    def test_invalid_limits_fail(self):
        for values in ((0,1,1),(-1,1,1),(math.nan,1,1),(math.inf,1,1),
                       (3601,1,1),(10,0,1),(10,31,1),(10,1,0),(10,1,31)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                gate.wait_ready(*values)

    def test_invalid_observation_limits_fail(self):
        for n in (True, 1, 11, 2.0):
            with self.subTest(n=n), self.assertRaises(ValueError):gate.wait_ready(observations=n)

    def test_no_sync_logs_neither_secret_nor_raw_probe(self):
        c = Clock(); c.run([False]); self.assertIn('No clock or license changed', c.outputs[-1])


class ProbeTests(unittest.TestCase):
    def result(self, code=0, text='yes\n'):
        return subprocess.CompletedProcess([],code,text,'private diagnostic')

    def probe(self, result):
        return gate.probe_sync(1, runner=lambda *a,**k:result, marker_check=lambda:True)

    def test_marker_is_required_even_with_kernel_synced(self):
        def forbidden(*a,**k):raise AssertionError('No command without a marker')
        self.assertFalse(gate.probe_sync(1, forbidden, lambda:False))

    def test_only_strict_yes_is_accepted(self):
        self.assertTrue(self.probe(self.result()))
        for text in ('no','true','1','','yes\nno','NTPSynchronized=yes'):
            with self.subTest(text=text):self.assertFalse(self.probe(self.result(text=text)))

    def test_failed_dbus_does_not_pass(self):self.assertFalse(self.probe(self.result(code=1)))

    def test_timeout_does_not_pass(self):
        def fail(*a,**k):raise subprocess.TimeoutExpired('timedatectl',1)
        self.assertFalse(gate.probe_sync(1,fail,lambda:True))

    def test_missing_command_does_not_pass(self):
        def fail(*a,**k):raise FileNotFoundError()
        self.assertFalse(gate.probe_sync(1,fail,lambda:True))

    def test_invalid_encoding_does_not_pass(self):
        def fail(*a,**k):raise UnicodeError()
        self.assertFalse(gate.probe_sync(1,fail,lambda:True))

    def test_command_has_no_mutating_arguments_or_shell(self):
        calls=[]
        def runner(args,**kw):calls.append((args,kw));return self.result()
        self.assertTrue(gate.probe_sync(1,runner,lambda:True))
        args,kw=calls[0]
        self.assertEqual(args,['/usr/bin/timedatectl','show','-p','NTPSynchronized','--value'])
        self.assertNotIn('shell',kw); self.assertEqual(kw['timeout'],1)


class MarkerTests(unittest.TestCase):
    def path(self,mode,uid=0):
        return types.SimpleNamespace(lstat=lambda:types.SimpleNamespace(st_mode=mode,st_uid=uid))

    def test_root_regular_file_is_valid(self):self.assertTrue(gate.marker_trusted(self.path(stat.S_IFREG|0o644)))
    def test_systemd_timesync_owner_is_valid(self):
        with patch.object(gate,'trusted_marker_uids',return_value={0,997}):
            self.assertTrue(gate.marker_trusted(self.path(stat.S_IFREG|0o644,997)))
    def test_unrelated_service_owner_is_not_valid(self):
        with patch.object(gate,'trusted_marker_uids',return_value={0,997}):
            self.assertFalse(gate.marker_trusted(self.path(stat.S_IFREG|0o644,998)))
    def test_missing_marker_is_rejected(self):
        def missing():raise FileNotFoundError()
        self.assertFalse(gate.marker_trusted(types.SimpleNamespace(lstat=missing)))
    def test_unprivileged_marker_is_rejected(self):self.assertFalse(gate.marker_trusted(self.path(stat.S_IFREG|0o644,1000)))
    def test_writable_marker_is_rejected(self):
        for mode in (0o666,0o664,0o622):self.assertFalse(gate.marker_trusted(self.path(stat.S_IFREG|mode)))
    def test_symlink_is_rejected(self):self.assertFalse(gate.marker_trusted(self.path(stat.S_IFLNK|0o777)))
    def test_directory_is_rejected(self):self.assertFalse(gate.marker_trusted(self.path(stat.S_IFDIR|0o755)))


class ConfigurationTests(unittest.TestCase):
    def test_dropin_appends_preflight_without_overriding_runtime(self):
        s=(ROOT/'99-max-radius-clock-ready.conf').read_text()
        self.assertIn('ExecStartPre=/usr/bin/python3 /usr/local/libexec/max-radius-clock-ready.py',s)
        self.assertNotIn('\nExecStart=',s);self.assertNotIn('\nExecStop=',s)
        self.assertNotIn('\nRestart=',s);self.assertNotIn('\nExecStartPre=\n',s)
    def test_gate_cannot_mutate_customer_or_clock(self):
        import ast
        t=ast.parse((ROOT/'clock_ready.py').read_text())
        modules=[n.names[0].name for n in ast.walk(t) if isinstance(n,ast.Import)]
        self.assertFalse(set(modules)&{'pymysql','sqlite3','docker','requests','paramiko'})
        self.assertNotIn('set-ntp',(ROOT/'clock_ready.py').read_text())
    def test_cli_rejects_nan(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as e:
            gate.main(['--wait-seconds','nan'])
        self.assertEqual(e.exception.code,2)


if __name__ == '__main__':unittest.main(verbosity=2)
