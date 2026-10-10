"""Container-side helpers for an isolated Chat deployment test, never live data."""
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    mode = sys.argv[1]
    root = Path('/run/chat-test')
    if mode == 'secrets':
        payload = json.load(sys.stdin)
        root.mkdir(exist_ok=True)
        for name in ['django.key', 'oidc.key', 'runtime.json']:
            value = payload[name]
            path = root / name
            path.write_text(value if isinstance(value, str) else json.dumps(value))
            path.chmod(0o600)
            os.chown(path, 10001, 10001)
        root.chmod(0o700)
        os.chown(root, 10001, 10001)
        print(json.dumps({'available_bytes': shutil.disk_usage('/app').free}))
        return
    environment = json.loads((root / 'runtime.json').read_text())
    if not environment['DATABASE_URL'].rsplit('/', 1)[-1].startswith('chat_test_'):
        raise RuntimeError('Only disposable Chat test databases are supported.')
    os.environ.update(environment)
    if mode in ['web', 'worker']:
        args = (['.venv/bin/gunicorn', 'core.wsgi:application', '--bind', '0.0.0.0:8000',
                 '--workers', '2', '--worker-tmp-dir', '/tmp'] if mode == 'web' else
                ['.venv/bin/python', 'manage.py', 'core_worker', '--interval', '1'])
        os.execv(args[0], args)
    os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings'
    sys.path.insert(0, '/app')
    import django
    django.setup()
    from django.core.management import call_command
    if mode == 'initialize':
        call_command('migrate', interactive=False, verbosity=0)
        call_command('sync_platform_docs', verbosity=0)
        call_command('check', verbosity=0)
        from identity.models import User
        payload = json.load(sys.stdin)
        for engine in ['chromium', 'firefox', 'webkit']:
            User.objects.create_superuser(f'{engine}-operator@example.test', payload['password'])
    elif mode == 'tests':
        call_command('test', 'core_chat.tests', 'identity.tests.test_csrf', 'platform_services.tests.test_header', interactive=False, verbosity=1)
    elif mode == 'header-fixture':
        # This branch is guarded by the disposable database check above. Values
        # are returned only to the harness's memory, never logs or live accounts.
        from datetime import timedelta
        import secrets
        from django.utils import timezone
        from oauth2_provider.models import AccessToken, set_token_value
        from identity.models import Membership, Organization, Role, ServicePermission
        from platform_services.models import Deployment, MarketplacePackage, RuntimeNode, WorkloadCredential
        from platform_services.services import install_package, issue_workload_credential, create_deployment
        from core_chat.models import Conversation, Participant
        payload = json.load(sys.stdin)
        organization = Organization.objects.get(slug=payload['slug'])
        membership = Membership.objects.get(organization=organization, user__email=payload['email'])
        deployment_environment = organization.environments.get(name='sandbox')
        role = Role.objects.get(organization=organization, name='Chat User')
        package, _ = MarketplacePackage.objects.get_or_create(slug='header-browser-app', defaults={
            'name': 'Review workspace', 'kind': 'application', 'description': 'Disposable browser fixture',
            'manifest': {'image': 'registry.example.test/review@sha256:' + 'c' * 64, 'capabilities': ['chat.read', 'chat.send']}})
        installation = install_package(organization, package, ['chat.read', 'chat.send'])
        permission, _ = ServicePermission.objects.get_or_create(service=installation.service, codename='app-read', defaults={'name': 'Read application'})
        role.permissions.add(permission)
        role.access_tags.add(deployment_environment.boundary_tag)
        node = RuntimeNode.objects.create(organization=organization, name='Header fixture', namespace=deployment_environment.namespace,
            environment=deployment_environment, credential_hash=WorkloadCredential.digest(secrets.token_urlsafe(48)))
        deployment = create_deployment(installation, node, payload['endpoint'])
        # No runtime agent is connected. No Kubernetes resources are provisioned.
        credential, workload_token = issue_workload_credential(installation, 'Header fixture', deployment_environment, deployment)
        user_token = secrets.token_urlsafe(48)
        token = AccessToken(application=deployment.application, user=membership.user, scope='openid authorization', expires=timezone.now() + timedelta(minutes=5))
        set_token_value(token, user_token)
        token.save()
        room = Conversation.objects.get(organization=organization, title='Laptop Test')
        room.permission_tags.add(deployment_environment.boundary_tag)
        Participant.objects.create(conversation=room, installation=installation)
        print(json.dumps({'organization_id': str(organization.pk), 'deployment_id': str(deployment.pk), 'room_id': str(room.pk),
            'workload_token': workload_token, 'user_token': user_token}))
    elif mode == 'state':
        from core_chat.models import Conversation, Message
        from platform_services.models import Deployment
        print(json.dumps({
            'conversations': Conversation.objects.count(), 'messages': Message.objects.count(),
            'chat': list(Deployment.objects.filter(installation__package__slug='core-chat').values(
                'observed_state', 'healthy', 'last_seen', 'revision', 'observed_revision')),
        }, default=str))
    else:
        raise RuntimeError('Unknown Chat test instance operation.')


if __name__ == '__main__':
    main()
