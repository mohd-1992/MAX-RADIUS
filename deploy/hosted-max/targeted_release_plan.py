"""Fail-closed preparation contract; this module cannot install or distribute images.

The 4k pilot is independent of the shared feed, latest tags and fixture releases.
Preparing a plan is not a deployment receipt or an installed-version assertion.
"""
import datetime as dt
import hashlib
import json
import re

DOMAIN = '4k.max-net.net'
PROJECT = '4k'
ROLES = {'web': 'web', 'core': 'radius_core'}
CERTIFICATES = frozenset({
    'hosted_web_runtime', 'web_core_pair', 'migration_preserves_entitlement',
    'rollback_after_new_accounting', 'rollback_runtime_license_lease',
    'durable_job_and_browser_progress', 'authenticated_single_target_delivery',
    'other_targets_denied', 'fresh_backup_and_preservation',
})


class Rejected(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _id(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def _image(value):
    return isinstance(value, str) and re.fullmatch('sha256:[0-9a-f]{64}', value)


def _timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError) as exc:
        raise Rejected('A timezone-aware timestamp is required') from exc


def prepare(domain, inventory, images, migration, now):
    """Bind two existing owned services and immutable candidate identities.

    The inventory is private evidence from full host inspect, not the proxy's
    display labels (which may label shared transport in a tenant's context).
    """
    if domain != DOMAIN or set(images) != set(ROLES):
        raise Rejected('Only the explicit 4k Web/Core pilot is supported')
    if any(not _image(v) for v in images.values()):
        raise Rejected('Immutable local image identities are required')
    if not isinstance(inventory, list) or not inventory:
        raise Rejected('Host inventory is required')
    live = [v for v in inventory if v.get('running') is True]
    services = {}
    for role, service in ROLES.items():
        found = [v for v in live if v.get('project') == PROJECT and v.get('service') == service]
        if len(found) != 1:
            raise Rejected('Ambiguous live tenant service')
        row = found[0]
        if not _id(row.get('id')) or not _image(row.get('image')) or row.get('health') != 'healthy':
            raise Rejected('Healthy current identity must be measured')
        if row.get('shared') or not row.get('networks'):
            raise Rejected('Shared or unmeasured service is excluded')
        if any(v.get('project') != PROJECT and set(v.get('networks', {})) & set(row['networks']) for v in live):
            raise Rejected('Foreign live service on tenant network requires review')
        services[role] = {k: row[k] for k in ('id', 'image', 'started', 'restarts')}
        services[role]['target_image'] = images[role]
        services[role]['bindings_sha256'] = digest({'networks': row['networks'], 'mounts': row.get('mounts', [])})
    if services['web']['id'] == services['core']['id']:
        raise Rejected('Web and Core must be separate owned services')
    if not isinstance(migration, dict) or migration.get('tested') is not True:
        raise Rejected('A measured clone migration result is required')
    if not migration.get('accounting_preserved') or not migration.get('original_sales_preserved'):
        raise Rejected('Accounting and existing financial values must be preserved')
    if not _id(migration.get('evidence_sha256')):
        raise Rejected('Migration evidence must be frozen')
    issued = _timestamp(now)
    plan = {
        'format': 1, 'version': '2.6.3', 'domain': DOMAIN, 'project': PROJECT,
        'issued_at': issued.isoformat(), 'expires_at': (issued + dt.timedelta(hours=24)).isoformat(),
        'services': services, 'migration': migration,
        'database_image_change': False, 'database_restore_on_rollback': False,
        'shared_services_change': False, 'automatic_install': False,
        'global_feed_change': False, 'latest_change': False,
        'inventory_sha256': digest(inventory), 'state': 'prepared_blocked',
        'required_certificates': sorted(CERTIFICATES),
    }
    plan['plan_sha256'] = digest(plan)
    return plan


def validate_current(plan, domain, inventory, now):
    unsigned = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    if digest(unsigned) != plan.get('plan_sha256') or domain != DOMAIN or plan.get('domain') != DOMAIN:
        raise Rejected('Plan integrity or target mismatch')
    current_time = _timestamp(now)
    if not _timestamp(plan['issued_at']) <= current_time < _timestamp(plan['expires_at']):
        raise Rejected('Plan is stale or from the future')
    if set(plan.get('services', {})) != set(ROLES):
        raise Rejected('Unexpected component scope')
    for role, service in ROLES.items():
        measured = [v for v in inventory if v.get('project') == PROJECT and v.get('service') == service and v.get('running') is True]
        if len(measured) != 1:
            raise Rejected('Current service is ambiguous')
        old = plan['services'][role]
        if any(measured[0].get(k) != old[k] for k in ('id', 'image', 'started', 'restarts')):
            raise Rejected('Current identity changed; a fresh scoped round is required')
        if measured[0].get('health') != 'healthy':
            raise Rejected('Current service is not healthy')
        row = measured[0]
        if row.get('shared') or digest({'networks': row.get('networks'), 'mounts': row.get('mounts', [])}) != old['bindings_sha256']:
            raise Rejected('Runtime network or storage binding changed')
        if any(v.get('running') is True and v.get('project') != PROJECT
               and set(v.get('networks', {})) & set(row['networks']) for v in inventory):
            raise Rejected('A foreign runtime joined the tenant network')


def readiness(plan, certificates):
    """Return missing measured gates. A source test never certifies live delivery.

    The caller must independently verify evidence files. This is a preparation
    gate, not an authority to execute an update or a cryptographic trust service.
    """
    if not isinstance(certificates, dict) or set(certificates) - CERTIFICATES:
        raise Rejected('Unknown readiness evidence')
    missing = []
    for name in sorted(CERTIFICATES):
        item = certificates.get(name, {})
        if not (item.get('passed') is True and item.get('plan_sha256') == plan['plan_sha256']
                and _id(item.get('evidence_sha256'))):
            missing.append(name)
    return {'ready': not missing, 'missing': missing, 'install_invoked': False,
            'published_to_clients': False, 'state': 'verified_preparation' if not missing else 'prepared_blocked'}


def public_status(plan, certificates):
    return {'version': plan['version'], 'domain': DOMAIN, 'components': sorted(ROLES),
            **readiness(plan, certificates)}
