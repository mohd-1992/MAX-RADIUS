"""Read-only local checks for signed panel requirements. No migration executor."""
import platform
import re

ENGINE_PROTOCOL = 4
CAPABILITIES = ['web','core','signed_audience','release_requirements_v1','migration']


def facts(config):
    arch={'x86_64':'amd64','AMD64':'amd64','aarch64':'arm64','arm64':'arm64'}.get(platform.machine())
    result={'engine_protocol':ENGINE_PROTOCOL}
    if platform.system()=='Linux' and arch:result['platform']='linux/'+arch
    # Only a host-provisioned, verified schema stamp; missing never means current.
    revision=config.get('schema_revision')
    if revision is not None:
        if not isinstance(revision,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,63}',revision):raise ValueError('Invalid trusted schema revision')
        result['schema_revision']=revision
    return result


def validate(requirements, config, components):
    keys={'engine_protocol','platforms','schema_revisions','migration_plan','rollback','release_kind','prepares_engine_protocol'}
    if not isinstance(requirements,dict) or set(requirements)!=keys:raise ValueError('Invalid signed requirements')
    protocol=requirements['engine_protocol'];observed=facts(config)
    if type(protocol) is not int or not 1<=protocol<=ENGINE_PROTOCOL:raise ValueError('Engine preparation required')
    platforms=requirements['platforms'];schemas=requirements['schema_revisions']
    if not isinstance(platforms,list) or any(not isinstance(v,str) for v in platforms) or observed.get('platform') not in platforms:raise ValueError('Platform not verified')
    if not isinstance(schemas,list) or any(not isinstance(v,str) for v in schemas) or observed.get('schema_revision') not in schemas:raise ValueError('Schema revision not verified')
    explicit = requirements['migration_plan'] is not None
    if explicit:
        from services.system_update_migrations import validate_plan
        spec = validate_plan(requirements['migration_plan'], components)
        if protocol < 3 or observed.get('schema_revision') != spec['from_schema']:
            raise ValueError('Explicit schema plan requires the prepared protocol3 executor')
    kind=requirements['release_kind'];target=requirements['prepares_engine_protocol']
    if kind=='preparation':
        if set(components)!={'web'} or type(target) is not int or not 2<=target<=ENGINE_PROTOCOL:raise ValueError('Unsupported engine preparation target')
    elif kind!='feature' or target is not None:raise ValueError('Invalid release kind')
    rollback=requirements['rollback']
    if not isinstance(rollback,dict) or set(rollback)!={'policy','images'} or rollback['policy']!='compatible':raise ValueError('Automatic rollback compatibility required')
    images=rollback['images']
    if not isinstance(images,dict) or set(images)!=set(components):raise ValueError('Missing compatible rollback images')
    for role,ref in images.items():
        if not isinstance(ref,str) or not re.fullmatch('mohd777/max-radius-'+role+r'@sha256:[a-f0-9]{64}',ref):raise ValueError('Invalid rollback image')
