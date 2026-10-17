#!/usr/bin/env python3
"""Explicit host-wide clock guard management; never restarts Docker or writes SQL."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import active_files
import clock_ready
import managed_files


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Host service metadata command failed')
    return result.stdout.strip()


def effective_guard():
    return command(['systemctl', 'show', 'docker.service', '-p', 'ExecStartPre', '--value'])


def reload_units():
    command(['systemctl', 'daemon-reload'])


def preflight():
    if not Path('/run/systemd/system').is_dir():
        raise RuntimeError('A systemd Linux host is required')
    if command(['systemctl', 'is-active', 'systemd-timesyncd.service']) != 'active':
        raise RuntimeError('An active systemd-timesyncd service is required; existing NTP configuration was left unchanged')
    if not clock_ready.wait_ready(wait_seconds=10, emit=lambda _: None):
        raise RuntimeError('Host time synchronization was not confirmed; no activation performed')
    if command(['systemctl', 'show', 'docker.service', '-p', 'LoadState', '--value']) != 'loaded':
        raise RuntimeError('The system Docker service was not found')


def enable(journal, root=Path('/'), check=preflight,
           reload=reload_units, loaded=effective_guard):
    root, journal = Path(root), Path(journal)
    check()
    if (journal / 'journal.json').exists():
        _, _, state = active_files.load(root, journal)
        if state['status'] == 'active':
            if active_files.HELPER not in loaded():
                raise RuntimeError('Recorded active guard is not loaded; preserve the journal for review')
            return state
        if state['status'] != 'staged-not-activated':
            raise RuntimeError('Previous activation requires explicit recovery; journal preserved')
    else:
        managed_files.stage(root, journal)
    try:
        return active_files.activate(root, journal, reload, loaded)
    except Exception:
        # The helper must survive until the loaded unit demonstrably forgets it.
        active_files.rollback_loaded(root, journal, reload, loaded)
        raise


def disable(journal, root=Path('/'), reload=reload_units, loaded=effective_guard):
    _, _, state = active_files.load(root, journal)
    if state['status'] in ('staged-not-activated', 'staging', 'rolling-back', 'rolled-back'):
        # An unrelated daemon-reload may have loaded a staged drop-in meanwhile.
        if active_files.HELPER not in loaded():
            return managed_files.rollback(root, journal)
        active_files.save(Path(journal), state, 'activating')
    return active_files.rollback_loaded(root, journal, reload, loaded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'enable', 'disable'])
    parser.add_argument('--journal', type=Path)
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps({'guard_loaded': active_files.HELPER in effective_guard(),
                          'ntp_confirmed': clock_ready.probe_sync(3)}))
        return
    if os.geteuid() != 0 or args.journal is None or not args.journal.is_absolute():
        parser.error('Root and an explicit absolute --journal are required')
    state = enable(args.journal) if args.action == 'enable' else disable(args.journal)
    print(json.dumps({'status': state['status'], 'service_restart_performed': False}))


if __name__ == '__main__':
    main()
