"""Explicit laptop configuration. Importing this module does not change settings."""
import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

from scripts.init_dev import initialize

ROOT = Path(__file__).resolve().parent.parent


class LocalConfigurationError(ValueError):
    pass


def port_number(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Use a port number between 1024 and 65535.')
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError('Use a port number between 1024 and 65535.')
    return port


def configure_local(port=None):
    # Check all configuration before creating files or touching a database.
    if os.environ.get('DATABASE_URL'):
        raise LocalConfigurationError('Local mode uses .local/db.sqlite3. Unset DATABASE_URL first; its value has not been printed or used.')
    if os.environ.get('CORE_DEBUG') not in (None, '', '1'):
        raise LocalConfigurationError('Local mode cannot override CORE_DEBUG. Unset it or set CORE_DEBUG=1 explicitly.')
    if os.environ.get('DJANGO_SETTINGS_MODULE') not in (None, '', 'core.settings'):
        raise LocalConfigurationError('Local mode requires core.settings. Unset DJANGO_SETTINGS_MODULE first.')
    if os.environ.get('CORE_TRUST_PROXY') == '1' or os.environ.get('CORE_OIDC_PREVIOUS_KEY_FILES'):
        raise LocalConfigurationError('Unset CORE_TRUST_PROXY and CORE_OIDC_PREVIOUS_KEY_FILES before using local mode.')
    supplied_origin = os.environ.get('CORE_ISSUER')
    if supplied_origin:
        try:
            parsed = urlparse(supplied_origin)
            selected_port = port_number(parsed.port or 80)
            if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'} or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path or parsed.query or parsed.fragment):
                raise ValueError
        except (ValueError, argparse.ArgumentTypeError):
            raise LocalConfigurationError('Local CORE_ISSUER must be an HTTP localhost or 127.0.0.1 origin with a port from 1024 to 65535; unset it to use the default.')
        if port is not None and port != selected_port:
            raise LocalConfigurationError('CORE_ISSUER does not match --port. Unset CORE_ISSUER or choose its port.')
        origin = supplied_origin
    else:
        selected_port = port_number(port if port is not None else 8000)
        origin = f'http://localhost:{selected_port}'
    if os.environ.get('CORE_ALLOWED_HOSTS'):
        hosts = {host.strip() for host in os.environ['CORE_ALLOWED_HOSTS'].split(',')}
        if not hosts <= {'localhost', '127.0.0.1', '[::1]'}:
            raise LocalConfigurationError('Local mode requires loopback CORE_ALLOWED_HOSTS. Unset CORE_ALLOWED_HOSTS first.')
    directory = ROOT / '.local'
    files = {'CORE_SECRET_KEY_FILE': directory / 'django.key', 'CORE_OIDC_KEY_FILE': directory / 'oidc.key'}
    for name, path in files.items():
        if os.environ.get(name) and Path(os.environ[name]).resolve() != path.resolve():
            raise LocalConfigurationError(f'Local mode preserves .local/ keys. Unset {name} to use them; the configured file has not been read or changed.')
    initialize(directory)
    os.environ.update(CORE_DEBUG='1', CORE_ISSUER=origin, CORE_ALLOWED_HOSTS='localhost,127.0.0.1,[::1]',
        DJANGO_SETTINGS_MODULE='core.settings', **{name: str(path) for name, path in files.items()})
    return origin, selected_port


def serve(arguments):
    parser = argparse.ArgumentParser(prog='python manage.py dev',
        description='Initialize local keys, apply migrations, synchronize bundled guides and start the loopback development server.')
    parser.add_argument('--port', type=port_number, help='Local port (default: 8000, or the configured local CORE_ISSUER port).')
    parser.add_argument('--noreload', action='store_true', help='Disable the development autoreloader.')
    options = parser.parse_args(arguments)
    origin, port = configure_local(options.port)
    import django
    from django.core.management import call_command, execute_from_command_line
    django.setup()
    # The autoreloader reexecutes the original command after setup; do not run
    # startup migrations twice or after every template/code change.
    if os.environ.get('RUN_MAIN') != 'true':
        call_command('migrate', interactive=False)
        call_command('sync_platform_docs')
        call_command('check')
        print(f'Local Core: {origin}/accounts/login/\nDatabase: .local/db.sqlite3. Existing keys and accounts are preserved.\nStop with Ctrl-C.', flush=True)
    command = [str(ROOT / 'manage.py'), 'runserver', f'127.0.0.1:{port}']
    if options.noreload:
        command.append('--noreload')
    execute_from_command_line(command)
