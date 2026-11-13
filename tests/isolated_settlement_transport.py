"""No external Docker/NAS access in the explicit synthetic MariaDB labs.

Keep the real SQL renewal guards, named locks, writes, rollback and receipts.
Only the container lookup and live NAS observation/delivery are synthetic.
Actual RADIUS packets are tested separately in test_license_capacity.
"""
import os
from contextlib import ExitStack, contextmanager
from unittest.mock import patch


@contextmanager
def isolated_settlement_transport():
    if (os.environ.get('DB_HOST') != 'max-license-test-db'
            or not os.environ.get('DB_NAME', '').startswith('subscriber_edit_fixture_')):
        raise RuntimeError('Explicit isolated settlement fixture required')
    with ExitStack() as stack:
        stack.enter_context(patch('services.renewal_settlement_service.current_radius_container',
                                  return_value='isolated-transport-fixture'))
        stack.enter_context(patch('services.online_policy_service.checkpoint', return_value={}))
        stack.enter_context(patch('services.online_policy_service.refresh_or_pending'))
        yield
