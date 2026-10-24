"""Control release protocol. No application imports or installation side effects."""
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CHANNEL = 'https://control.max-net.net'
MAX_BYTES = 262144
CAPABILITIES = ['web', 'core', 'signed_audience']


class ChannelError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ChannelError('Invalid version')
    return tuple(map(int, value.split('.')))


def configuration(storage):
    path = Path(storage) / 'update-channel.private.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('kind') not in ('public', 'domain', 'local'):
        raise ChannelError('Invalid installation kind')
    if value.get('kind') != 'public':
        if not all(isinstance(value.get(k), str) and value[k] for k in ('identity', 'reader_id', 'token')):
            raise ChannelError('Installation is not enrolled')
    base = urllib.parse.urlsplit(value.get('channel', CHANNEL))
    if base.scheme != 'https' or not base.hostname or base.username or base.password or base.query or base.fragment:
        raise ChannelError('Channel requires an HTTPS origin')
    value['channel'] = value.get('channel', CHANNEL).rstrip('/')
    # Trust is installed out of band; it never comes from /key or the feed.
    key = base64.b64decode(value['public_key'], validate=True)
    if len(key) != 32 or hashlib.sha256(key).hexdigest() != value['key_fingerprint']:
        raise ChannelError('Pinned channel trust is invalid')
    if value.get('executor') not in ('host', 'local'):
        raise ChannelError('Installation executor is not configured')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward an installation's Bearer credential to another endpoint.
        raise ChannelError('Channel redirects are not accepted')


def verify_response(response, config, current, now=None, capabilities=None):
    version(current)
    if not isinstance(response, dict) or response.get('success') is not True or response.get('current_version') != current:
        raise ChannelError('Invalid feed response')
    if response.get('has_update') is False:
        return None
    if response.get('has_update') is not True:
        raise ChannelError('Missing availability decision')
    key = base64.b64decode(config['public_key'], validate=True)
    if response.get('key_fingerprint') != hashlib.sha256(key).hexdigest():
        raise ChannelError('Channel key changed')
    verifier = Ed25519PublicKey.from_public_bytes(key)
    manifest, offer = response['manifest'], response['offer']
    verifier.verify(base64.b64decode(response['signature'], validate=True), canonical(manifest))
    verifier.verify(base64.b64decode(response['offer_signature'], validate=True), canonical(offer))
    if manifest.get('schema') != 1 or version(manifest['version']) <= version(current):
        raise ChannelError('Downgrade or unsupported manifest')
    if version(manifest['min_client_version']) > version(current):
        raise ChannelError('Client is below the minimum supported version')
    audience = manifest['audience']
    if audience != {'kind': 'public'}:
        if (config['kind'] == 'public' or audience.get('kind') != config['kind']
                or audience.get('id') != config['reader_id'] or audience.get('identity') != config['identity']):
            raise ChannelError('Release audience mismatch')
    reader = 'public' if config['kind'] == 'public' else config['reader_id']
    if (offer.get('reader') != reader or offer.get('current_version') != current
            or offer.get('release') != manifest.get('release')
            or offer.get('manifest_sha256') != hashlib.sha256(canonical(manifest)).hexdigest()):
        raise ChannelError('Offer does not belong to this installation and manifest')
    issued = dt.datetime.fromisoformat(offer['issued_at'])
    expires = dt.datetime.fromisoformat(offer['valid_until'])
    now = now or dt.datetime.now(dt.timezone.utc)
    if issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires or not 0 < (expires-issued).total_seconds() <= 300:
        raise ChannelError('Offer is expired or has an invalid lifetime')
    images = manifest['images']
    if not isinstance(images, dict) or not images or 'web' not in images or set(images)-{'web', 'core'}:
        raise ChannelError('Unsupported components')
    if sorted(images) != manifest.get('components') or not set(images) <= set(capabilities or CAPABILITIES):
        raise ChannelError('Components do not match executor capabilities')
    for role, image in images.items():
        if not isinstance(image, str) or not re.fullmatch('mohd777/max-radius-'+role+r'@sha256:[0-9a-f]{64}', image):
            raise ChannelError('Unpinned or unapproved repository')
    # A free-text migration/rollback description is not executable authorization.
    # First consumer supports backward-compatible releases without explicit DDL.
    if manifest.get('migration') != 'none':
        raise ChannelError('This release requires a separately supported migration')
    expected_scope = 'web_only' if set(images) == {'web'} else 'web_core'
    if (manifest.get('update_scope') != expected_scope or manifest.get('automatic_install') is not False
            or manifest.get('database_image_change') is not False or manifest.get('shared_services_change') is not False):
        raise ChannelError('Release exceeds permitted update scope')
    return manifest


def fetch(config, current, capabilities=None):
    caps = capabilities or CAPABILITIES
    path = '/api/releases/v1/latest' if config['kind'] == 'public' else '/api/releases/v1/check'
    url = config['channel'] + path + '?' + urllib.parse.urlencode({'current_version': current, 'capabilities': ','.join(caps)})
    headers = {'User-Agent': 'MAX-RADIUS/'+current, 'Accept': 'application/json'}
    if config['kind'] != 'public':
        headers['Authorization'] = 'Bearer '+config['token']
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
        if response.status != 200 or response.geturl() != url:
            raise ChannelError('Unexpected channel response')
        raw = response.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ChannelError('Feed exceeds the size limit')
    result = json.loads(raw)
    verify_response(result, config, current, capabilities=caps)
    return result


def public_result(response, config, current):
    manifest = verify_response(response, config, current)
    result = {'success': True, 'has_update': bool(manifest), 'current_version': current,
              'latest_version': manifest['version'] if manifest else None,
              'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'automatic_install': False,
              'message': 'تم التحقق من قناة التحديثات؛ لا يوجد إصدار أحدث متوافق.'}
    if manifest:
        result.update(title=manifest['title'], changelog=manifest['changelog'], components=manifest['components'],
                      release_date=manifest['published'], release_id=manifest['release'],
                      manifest_sha256=response['offer']['manifest_sha256'], migration=manifest['migration'],
                      message='يتوفر إصدار موثق ومتوافق مع هذه النسخة.')
    return result
