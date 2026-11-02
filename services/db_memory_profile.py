"""Prepare a reversible host-side configuration; never contact Docker or SQL.

Memory profiles are per engine, including all tenants on a shared engine. The
caller must select and verify the engine; Web-only updates cannot activate this.
"""
import copy
import re
import hashlib

CONFIG='[mariadb]\n# Opt-in profile: leave InnoDB, durability, connections and tenant data unchanged.\naria_pagecache_buffer_size=16M\nkey_buffer_size=8M\n'


def plan(service, allocator, shared=False, participants=(), approved_participants=(), profile='balanced'):
    if profile not in ('balanced','conservative'): raise ValueError('Unknown memory profile')
    image=str(service.get('image',''))
    if not re.search(r'(?:^|/)mariadb:(?:10\.11|11\.8)(?:[.@-]|$)',image):
        raise ValueError('Memory profile requires a verified supported MariaDB image')
    if allocator!='glibc': raise ValueError('Allocator must be verified as glibc')
    if shared and (not participants or set(participants)!=set(approved_participants)):
        raise ValueError('A shared engine requires the complete approved tenant set')
    current=service.get('command',[])
    text=' '.join(current) if isinstance(current,list) else str(current)
    if profile=='conservative' and re.search(r'(aria[-_]pagecache[-_]buffer[-_]size|key[-_]buffer[-_]size)',text):
        raise ValueError('Existing CLI cache settings must be reviewed before changing the profile')
    after=copy.deepcopy(service)
    env=after.get('environment',{})
    if isinstance(env,list):
        pairs=[str(x).split('=',1) for x in env]
        if any(len(x)!=2 for x in pairs): raise ValueError('Inherited environment requires explicit review')
        env=dict(pairs)
    else: env=dict(env)
    if 'MALLOC_ARENA_MAX' in env and str(env['MALLOC_ARENA_MAX'])!='2':
        raise ValueError('Existing allocator policy requires explicit review')
    env['MALLOC_ARENA_MAX']='2';after['environment']=env
    if profile=='balanced':
        return dict(before=copy.deepcopy(service),after=after,shared=shared,participants=sorted(set(participants)),
                    requires_restart=True,changes_data=False,automatic_apply=False,profile='balanced-v1',
                    managed_config=None,config_sha256=None,
                    note='Only glibc arenas change. Restore before to revert; existing caches are preserved.')
    mounts=list(after.get('volumes',[]))
    expected='./deploy/db-memory/conservative.cnf:/etc/mysql/conf.d/max-memory.cnf:ro'
    for value in mounts:
        if value==expected: continue
        if isinstance(value,dict) and value.get('target') in ('/etc/mysql/conf.d','/etc/mysql/conf.d/max-memory.cnf'):
            raise ValueError('Conflicting MariaDB configuration mount')
        if isinstance(value,str) and re.search(r':/etc/mysql/conf\.d(?:/max-memory\.cnf)?(?::|$)',value):
            raise ValueError('Conflicting MariaDB configuration mount')
    if expected not in mounts: mounts.append(expected)
    after['volumes']=mounts
    return dict(before=copy.deepcopy(service),after=after,shared=shared,participants=sorted(set(participants)),
                requires_restart=True,changes_data=False,automatic_apply=False,
                profile='conservative-v1',managed_config=CONFIG,config_sha256=hashlib.sha256(CONFIG.encode()).hexdigest(),
                note='InnoDB/durability/ports/networks and image are preserved. Restore before to revert.')
