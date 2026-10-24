"""Pure validation shared by Core and the pinned deployment runner.

Application metadata never selects templates, roles, secrets, commands or ports.
The Fargate recipe accepts only reviewed image digests from operator-approved ECR
repositories and an explicit application permission vocabulary.
"""
import json
import re


class ContractError(ValueError):
    pass


def identity_contract(value):
    if not isinstance(value, dict) or set(value) != {'callback_path', 'permissions'}:
        raise ContractError('Application identity requires a callback path and permissions.')
    path = value['callback_path']
    if not isinstance(path, str) or not re.fullmatch(r'/[a-zA-Z0-9/_-]{1,199}', path) or path.startswith('//'):
        raise ContractError('Use a simple absolute application callback path.')
    if path != '/api/auth/core/callback':
        raise ContractError('The fixed container recipe requires /api/auth/core/callback.')
    permissions = value['permissions']
    if not isinstance(permissions, list) or not 1 <= len(permissions) <= 32:
        raise ContractError('Declare between one and 32 application permissions.')
    names = set()
    for permission in permissions:
        if (not isinstance(permission, dict) or set(permission) != {'codename', 'name'} or
                not isinstance(permission['codename'], str) or
                not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', permission['codename']) or
                permission['codename'] == 'admin' or permission['codename'] in names or
                not isinstance(permission['name'], str) or not permission['name'].strip() or
                len(permission['name']) > 160 or any(ord(c) < 32 for c in permission['name'])):
            raise ContractError('Application permissions require unique nonreserved names.')
        names.add(permission['codename'])
    if len(json.dumps(value).encode()) > 8192:
        raise ContractError('Application identity exceeds the receipt size limit.')
    return value


def fargate_applications(config, account, region, repositories, prefix=None):
    apps = config.get('fargate_apps', {})
    if not isinstance(apps, dict) or len(apps) > 20:
        raise ContractError('Fargate application settings must be a bounded repository map.')
    keys = {'image_repository', 'browser_image_repository', 'runtime_secret_arn', 'certificate_arn', 'api_host'}
    for repository, values in apps.items():
        if repository not in repositories or not isinstance(values, dict) or set(values) != keys:
            raise ContractError('Configure Fargate resources only for an approved repository.')
        for key in ('image_repository', 'browser_image_repository'):
            if not isinstance(values[key], str) or not re.fullmatch(
                    rf'{re.escape(account)}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]{{0,255}}', values[key]):
                raise ContractError('Fargate images require an exact ECR repository in the platform account and region.')
            if prefix and not values[key].split('/', 1)[1].startswith(prefix + '-'):
                raise ContractError('Fargate image repositories must use the platform resource prefix.')
        for key, service, suffix in [('runtime_secret_arn', 'secretsmanager', r'secret:[A-Za-z0-9/_+=.@-]{1,256}'),
                                     ('certificate_arn', 'acm', r'certificate/[a-f0-9-]{36}')]:
            if not isinstance(values[key], str) or not re.fullmatch(
                    rf'arn:aws:{service}:{re.escape(region)}:{re.escape(account)}:{suffix}', values[key]):
                raise ContractError('Fargate resource ARNs must belong to the platform account and region.')
        if prefix and not values['runtime_secret_arn'].split(':secret:', 1)[1].startswith(prefix + '-'):
            raise ContractError('Fargate runtime secret must use the platform resource prefix.')
        if not isinstance(values['api_host'], str) or not re.fullmatch(
                r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', values['api_host']):
            raise ContractError('Fargate API host must be a DNS hostname.')
    return apps


def fargate_backend(backend, approved=None):
    if not isinstance(backend, dict) or set(backend) != {'image', 'browser_image'}:
        raise ContractError('Fargate backend requires API and isolated browser image digests only.')
    for key, repository_key in [('image', 'image_repository'), ('browser_image', 'browser_image_repository')]:
        image = backend[key]
        if not isinstance(image, str) or not re.fullmatch(
                r'[0-9]{12}\.dkr\.ecr\.[a-z]{2}(?:-[a-z]+)+-[0-9]\.amazonaws\.com/[a-z0-9][a-z0-9._/-]{0,255}@sha256:[a-f0-9]{64}', image):
            raise ContractError('Fargate image references must use immutable private ECR digests.')
        if approved and image.rsplit('@', 1)[0] != approved[repository_key]:
            raise ContractError('Fargate image is outside the approved application repository.')
    return backend


def academy_fargate_applications(config, account, region, repositories, prefix=None):
    """Academy has one API image and no browser execution authority."""
    apps = config.get('academy_fargate_apps', {})
    if not isinstance(apps, dict) or len(apps) > 20:
        raise ContractError('Academy resources must be a bounded repository map.')
    keys = {'image_repository', 'runtime_secret_arn', 'certificate_arn', 'api_host'}
    for values in apps.values():
        if (not isinstance(values, dict) or set(values) - {'allow_live_models', 'allow_agent_teachers'} != keys
                or any(type(values.get(flag, False)) is not bool
                       for flag in ('allow_live_models', 'allow_agent_teachers'))):
            raise ContractError('Academy resources require one ECR repository and approved runtime references.')
    normalized = {repository: {**{key: item for key, item in values.items() if key not in {'allow_live_models', 'allow_agent_teachers'}},
                              'browser_image_repository': values['image_repository']}
                  for repository, values in apps.items()}
    fargate_applications({'fargate_apps': normalized}, account, region, repositories, prefix)
    return apps


def academy_fargate_backend(backend, approved=None):
    if not isinstance(backend, dict) or set(backend) != {'image'}:
        raise ContractError('Academy backend requires one immutable API image only.')
    normalized = {'image': backend['image'], 'browser_image': backend['image']}
    approval = ({**approved, 'browser_image_repository': approved['image_repository']} if approved else None)
    fargate_backend(normalized, approval)
    return backend


def game_studio_identity():
    """Core owns this vocabulary. Repository manifests cannot add authority."""
    return {'callback_path': '/oauth/callback', 'permissions': [
        {'codename': 'games-play', 'name': 'Play published games and save shared arcade scores'},
        {'codename': 'games-create', 'name': 'Administer game creation and private drafts'},
        {'codename': 'games-publish', 'name': 'Administer game publication and editions'},
        {'codename': 'games-manage', 'name': 'Manage Game Studio content, branding and creation settings'},
        {'codename': 'agents-manage', 'name': 'Configure Game Studio agents and workflows'},
        {'codename': 'accounts-manage', 'name': 'Manage Game Studio accounts and invitations'}],
        'account_roles': [
            {'id': 'player', 'name': 'Player', 'description': 'Play published games and save shared arcade scores without game creation access.', 'permissions': ['games-play']},
            {'id': 'creator', 'name': 'Game creation administrator', 'description': 'Create private games and review isolated playtests.', 'permissions': ['games-create']},
            {'id': 'publisher', 'name': 'Game publishing administrator', 'description': 'Create, playtest and publish reviewed game editions.', 'permissions': ['games-create', 'games-publish']},
            {'id': 'administrator', 'name': 'Game Studio administrator', 'description': 'Manage games, branding, creation settings, agents and application accounts.',
                'permissions': ['games-create', 'games-publish', 'games-manage', 'agents-manage', 'accounts-manage']}]}


def game_studio_configuration(value):
    """Only operator-reviewed HTTPS host references and plain branding enter AWS."""
    from urllib.parse import urlsplit
    def require(condition):
        if not condition: raise ContractError('Game Studio requires bounded branding and exact HTTPS embedding hosts.')
    def label(item, maximum):
        require(isinstance(item, str) and 1 <= len(item) <= maximum and item == item.strip() and not re.search(r'[<>\x00-\x1f\x7f-\x9f]', item))
    def url(item, origin=False):
        require(isinstance(item, str) and len(item) <= 2048 and not re.search(r'[\s\\<>\x00-\x1f\x7f-\x9f]', item))
        try:
            parsed = urlsplit(item); port = parsed.port
        except ValueError: raise ContractError('Invalid Game Studio host URL.') from None
        require(parsed.scheme == 'https' and bool(parsed.hostname) and parsed.username is None and parsed.password is None and '?' not in item and '#' not in item)
        require(not origin or not parsed.path)
        require(port is None or 1 <= port <= 65535)
        require(bool(re.fullmatch(r'[A-Za-z0-9.\-:]+', parsed.hostname or '')) and '..' not in parsed.hostname)
        if origin:
            canonical = 'https://' + parsed.hostname + (':' + str(port) if port is not None and port != 443 else '')
            require(item == canonical)
            if re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', parsed.hostname.rsplit('.', 1)[-1], re.I):
                import ipaddress
                try: require(str(ipaddress.ip_address(parsed.hostname)) == parsed.hostname)
                except ValueError: raise ContractError('Use a canonical HTTPS embedding origin.') from None
    require(isinstance(value, dict) and set(value) == {'version', 'branding', 'hosts'} and type(value['version']) is int and value['version'] == 1)
    branding = value['branding']
    require(isinstance(branding, dict) and {'appName', 'badgeText', 'accentColor'} <= set(branding) and not set(branding) - {'appName', 'badgeText', 'accentColor', 'logoUrl'})
    label(branding['appName'], 80); label(branding['badgeText'], 80)
    require(isinstance(branding['accentColor'], str) and bool(re.fullmatch(r'#[a-fA-F0-9]{6}', branding['accentColor'])))
    if 'logoUrl' in branding: url(branding['logoUrl'])
    require(isinstance(value['hosts'], list) and len(value['hosts']) <= 20)
    ids, origins = set(), set()
    for host in value['hosts']:
        require(isinstance(host, dict) and set(host) == {'id', 'name', 'origin', 'returnPath'})
        require(isinstance(host['id'], str) and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', host['id'])) and host['id'] not in ids)
        label(host['name'], 80); url(host['origin'], origin=True)
        require(host['origin'] not in origins)
        path = host['returnPath']
        require(isinstance(path, str) and len(path) <= 500 and bool(re.fullmatch(r'/(?:[A-Za-z0-9._~-]+/?)*', path)) and not any(part in {'.', '..'} for part in path.split('/')))
        ids.add(host['id']); origins.add(host['origin'])
    # Leave room for Core IDs, table names and credential references within the
    # Lambda environment limit. This map contains no model/service credentials.
    require(len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()) <= 2048)
    return value


def game_studio_public_configuration(value):
    result = {key: value[key] for key in ('version', 'branding', 'hosts')}
    return game_studio_configuration(result)


def game_studio_applications(config, repositories, account=None, region=None):
    apps = config.get('game_studio_apps', {})
    if not isinstance(apps, dict) or len(apps) > 20:
        raise ContractError('Game Studio settings must be a bounded approved-repository map.')
    for repository, values in apps.items():
        if (repository not in repositories or not isinstance(values, dict) or
                not {'version', 'branding', 'hosts'} <= set(values) or
                set(values) - {'version', 'branding', 'hosts', 'allow_paid_models', 'bedrock_model_arns'}):
            raise ContractError('Configure Game Studio integration only for an approved repository.')
        game_studio_public_configuration(values)
        models, enabled = values.get('bedrock_model_arns', []), values.get('allow_paid_models', False)
        if (type(enabled) is not bool or not isinstance(models, list) or len(models) > 20 or
                any(not isinstance(arn, str) for arn in models) or len(set(models)) != len(models) or enabled and not models):
            raise ContractError('Paid Game Studio models require explicit operator approval and exact unique model ARNs.')
        for arn in models:
            match = re.fullmatch(r'arn:aws:bedrock:([a-z]{2}(?:-[a-z]+)+-[0-9]):([0-9]{12})?:(foundation-model|inference-profile|application-inference-profile)/[A-Za-z0-9._:-]{1,512}', arn)
            if not match or (match[3] == 'foundation-model' and match[2]) or (match[3] != 'foundation-model' and (not match[2] or account and match[2] != account)):
                raise ContractError('Game Studio model policies require exact foundation-model or platform-account inference-profile ARNs, without wildcards.')
    return apps


def game_studio_hosting_headers(value):
    """Trusted runner overwrites source-provided headers at publication."""
    game_studio_configuration(value)
    rules = [('**', [('Strict-Transport-Security', 'max-age=31536000'), ('X-Content-Type-Options', 'nosniff'),
        ('Referrer-Policy', 'no-referrer'), ('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')])]
    rules.append(('/sdk/**', [('Access-Control-Allow-Origin', '*'), ('Cross-Origin-Resource-Policy', 'cross-origin')]))
    for pattern in ['/', '/index.html', '/games', '/games/**', '/game-studio', '/game-studio/**', '/studio', '/studio/**',
                    '/oauth/**', '/settings', '/settings/**', '/accounts', '/accounts/**', '/agents', '/agents/**', '/feedback', '/feedback/**']:
        rules.append((pattern, [('X-Frame-Options', 'DENY'), ('Content-Security-Policy', "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"), ('Cache-Control', 'no-store')]))
    ancestors = ' '.join(host['origin'] for host in value['hosts']) or "'none'"
    rules.append(('/embed/games/**', [('Content-Security-Policy', f"frame-ancestors {ancestors}; object-src 'none'; base-uri 'self'"), ('Cache-Control', 'no-store')]))
    return 'customHeaders:\n' + ''.join('  - pattern: ' + json.dumps(pattern) + '\n    headers:\n' + ''.join(
        '      - key: ' + json.dumps(key) + '\n        value: ' + json.dumps(item) + '\n' for key, item in headers) for pattern, headers in rules)
