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
        if (not isinstance(values, dict) or set(values) - {'allow_live_models'} != keys
                or not isinstance(values.get('allow_live_models', False), bool)):
            raise ContractError('Academy resources require one ECR repository and approved runtime references.')
    normalized = {repository: {**{key: item for key, item in values.items() if key != 'allow_live_models'},
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
