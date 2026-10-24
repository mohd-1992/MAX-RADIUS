"""2.6.4 update entrypoints. Control consumers never fall back to unsigned feeds."""
from core.config import APP_VERSION
from services import system_update_control as control


def get_current_system_version():
    return APP_VERSION


def check_for_updates(force_refresh=False, force=False):
    return control.check(APP_VERSION)


def get_update_progress():
    return control.progress()


def trigger_system_update(target_version=None, backup_first=True, triggered_by='Admin',
                          release_id=None, manifest_sha256=None):
    return control.apply(target_version, backup_first, release_id, manifest_sha256)
