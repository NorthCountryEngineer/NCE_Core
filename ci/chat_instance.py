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
        call_command('test', 'core_chat.tests', 'identity.tests.test_csrf', interactive=False, verbosity=1)
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
