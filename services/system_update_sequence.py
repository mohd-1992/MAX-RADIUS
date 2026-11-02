"""Completion gating and read-only attestation for the 265 -> 268 bridge."""


def gate(result, progress):
    if (progress.get('status') not in ('idle', 'completed')
            and result.get('latest_version') != progress.get('target_version')):
        return dict(success=True, has_update=False,
                    current_version=result['current_version'], latest_version=None,
                    automatic_install=False, checked_at=result.get('checked_at'),
                    message='يلزم اكتمال المرحلة الحالية والتحقق منها قبل عرض التحديث التالي.')
    return result


BASELINE_PROBE = '''import json
from database.db import get_connection,query_one
from database.schema_objects import schema_object_matches
from database.license_capacity import CHECK_SQL,RESERVE_SQL
with get_connection() as c:
 with c.cursor() as cur:matched=all(schema_object_matches(cur,v) for v in (CHECK_SQL,RESERVE_SQL))
mac=query_one("SELECT COUNT(*) n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('wisp_account_mac_policies','wisp_account_mac_addresses')")['n']
print(json.dumps({'verified':bool(matched and mac==0),'schema_revision':'features267'}))
'''


def persist_schema(plan, folder):
    import hashlib
    import json
    from services.system_update_runtime import UpdateError, write_json
    channel = folder.parent.parent/'update-channel.private.json'
    if channel.is_symlink() or hashlib.sha256(channel.read_bytes()).hexdigest() != plan['channel_sha256']:
        raise UpdateError('Trusted channel changed during schema preparation')
    config = json.loads(channel.read_text())
    if config['project'] != plan['project']:
        raise UpdateError('Schema preparation owner changed')
    config['schema_revision'] = 'features267'
    controller = plan.get('controller')
    if controller:
        config['executor_image'] = plan['references']['web']
        if controller.get('legacy_socket_mount'):
            config['controller_mounts'].append(controller['legacy_socket_mount'])
    write_json(channel, config)
    write_json(folder/'schema-preparation.private.json', plan['schema_preparation'])
