#!/usr/bin/env python3
"""Read-only boot gate for hosts managed by systemd-timesyncd.

No database, Docker, network, license, RTC or wall-clock mutation is performed.
The /run marker and the kernel synchronization flag must agree twice.
"""
import argparse
import math
from pathlib import Path
import stat
import subprocess
import sys
import time

SYNC_MARKER = Path('/run/systemd/timesync/synchronized')
TIMEDATECTL = '/usr/bin/timedatectl'


def trusted_marker_uids():
    uids = {0}
    try:
        import pwd
        uids.add(pwd.getpwnam('systemd-timesync').pw_uid)
    except (ImportError, KeyError, OSError):
        pass
    return uids


def marker_trusted(path=SYNC_MARKER):
    try:
        info = path.lstat()
        return (stat.S_ISREG(info.st_mode) and info.st_uid in trusted_marker_uids()
                and not info.st_mode & 0o022)
    except OSError:
        return False


def probe_sync(timeout, runner=subprocess.run, marker_check=marker_trusted):
    if not marker_check():
        return False
    try:
        result = runner(
            [TIMEDATECTL, 'show', '-p', 'NTPSynchronized', '--value'],
            capture_output=True, text=True, timeout=timeout, check=False,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'},
        )
        return result.returncode == 0 and result.stdout.strip() == 'yes'
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def wait_ready(wait_seconds=180.0, poll_seconds=2.0, probe_timeout=3.0,
               observations=2, monotonic=time.monotonic, sleep=time.sleep,
               probe=probe_sync, emit=print, wall_clock=time.time,
               max_step_seconds=0.5):
    values = (wait_seconds, poll_seconds, probe_timeout, max_step_seconds)
    if (any(not math.isfinite(v) or v <= 0 for v in values)
            or wait_seconds > 3600 or poll_seconds > 30 or probe_timeout > 30
            or max_step_seconds > 5
            or type(observations) is not int or not 2 <= observations <= 10):
        raise ValueError('Invalid clock gate limits')
    deadline = monotonic() + wait_seconds
    consecutive = 0
    previous = None
    emit('MAX clock gate: waiting for confirmed systemd time synchronization.')
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            emit('MAX clock gate: time unconfirmed; service start deferred. No clock or license changed.')
            return False
        ready = probe(min(probe_timeout, remaining))
        sample_mono, sample_wall = monotonic(), wall_clock()
        stable = math.isfinite(sample_mono) and math.isfinite(sample_wall)
        if previous is not None:
            mono_elapsed=sample_mono-previous[0]
            wall_elapsed=sample_wall-previous[1]
            stable=stable and mono_elapsed>=0 and abs(wall_elapsed-mono_elapsed)<=max_step_seconds
        previous=(sample_mono,sample_wall) if stable else None
        # A probe must not authorize a service after its deadline.
        if monotonic() >= deadline:
            emit('MAX clock gate: confirmation deadline reached; service start deferred.')
            return False
        if ready and not stable:
            emit('MAX clock gate: clock step observed; confirmation window restarted.')
        consecutive = consecutive + 1 if ready and stable else 0
        if consecutive >= observations:
            emit('MAX clock gate: confirmed time synchronization; service start permitted.')
            return True
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(poll_seconds, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-seconds', type=float, default=180)
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--probe-timeout', type=float, default=3)
    args = parser.parse_args(argv)
    try:
        ready = wait_ready(args.wait_seconds, args.poll_seconds, args.probe_timeout)
    except ValueError:
        parser.error('Limits must be finite, positive and within documented bounds.')
    return 0 if ready else 75


if __name__ == '__main__':
    sys.exit(main())
