"""Portable process launcher for the preserved production Core application.

This changes deployment plumbing only. Migrations remain an explicit release
operation; neither web nor worker startup mutates the production schema.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def configure(environ=None, secret_root=None):
    environ = os.environ if environ is None else environ
    root = Path(secret_root or "/tmp/core-secrets")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for source, target, filename in [
        ("CORE_DJANGO_KEY", "CORE_SECRET_KEY_FILE", "django.key"),
        ("CORE_OIDC_PRIVATE_KEY", "CORE_OIDC_KEY_FILE", "oidc.key"),
        ("CORE_ORIGIN_SECRET", "CORE_ORIGIN_SECRET_FILE", "origin.key"),
    ]:
        value = environ.pop(source, "")
        mounted = environ.get(target, "")
        if value:
            path = root / filename
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(value)
            path.chmod(0o600)
            environ[target] = str(path)
        elif mounted:
            try:
                if not Path(mounted).read_text().strip():
                    raise ValueError(f"Empty mounted secret {target}")
            except OSError:
                raise ValueError(f"Unreadable mounted secret {target}") from None
        elif source != "CORE_ORIGIN_SECRET" or environ.get("CORE_AWS_ORIGIN_BOUNDARY") == "1":
            raise ValueError(f"Missing runtime secret {source} or {target}")
    database = environ.get("DATABASE_URL", "")
    parsed = urlsplit(database)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("DATABASE_URL must identify the preserved PostgreSQL database")
    environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")


def command(arguments):
    action = arguments[0] if arguments else "web"
    if action == "web":
        return [".venv/bin/gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000",
                "--workers", "1", "--threads", "2", "--timeout", "60", "--error-logfile", "-"]
    if action == "worker":
        return [".venv/bin/python", "manage.py", "core_worker"]
    if action == "manage" and len(arguments) > 1:
        return [".venv/bin/python", "manage.py", *arguments[1:]]
    if action == "idle":
        return [".venv/bin/python", "-c", "import time; time.sleep(86400)"]
    raise ValueError("Expected web, worker, idle or manage <management command>")


if __name__ == "__main__":
    try:
        configure()
        selected = command(sys.argv[1:])
        if not sys.argv[1:] or sys.argv[1] == "web":
            import subprocess
            subprocess.run([".venv/bin/python", "manage.py", "collectstatic", "--noinput"], check=True)
        os.execv(selected[0], selected)
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
