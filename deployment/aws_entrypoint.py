"""ECS entrypoint. Never migrates during a service start; releases migrate once."""
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode


def configure(environ=None, secret_root=None):
    environ = os.environ if environ is None else environ
    secret_root = Path(secret_root or "/tmp/core-secrets")
    secret_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_root.chmod(0o700)
    for source, target, filename in [
        ("CORE_DJANGO_KEY", "CORE_SECRET_KEY_FILE", "django.key"),
        ("CORE_OIDC_PRIVATE_KEY", "CORE_OIDC_KEY_FILE", "oidc.key"),
        ("CORE_ORIGIN_SECRET", "CORE_ORIGIN_SECRET_FILE", "origin.key"),
    ]:
        value = environ.pop(source, "")
        if not value:
            raise ValueError(f"Missing ECS secret {source}")
        path = secret_root / filename
        # Files live on the container's ephemeral filesystem, readable only by UID 10001.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
        path.chmod(0o600)
        environ[target] = str(path)
    password = environ.pop("CORE_DB_PASSWORD", "")
    if not password:
        raise ValueError("Missing ECS secret CORE_DB_PASSWORD")
    options = urlencode({"sslmode": "verify-full", "sslrootcert": environ.get(
        "CORE_RDS_CA_FILE", "/app/deployment/rds-global-bundle.pem")})
    environ["DATABASE_URL"] = "postgresql://{}:{}@{}:{}/{}?{}".format(
        quote(environ.get("CORE_DB_USER", "ncecore"), safe=""), quote(password, safe=""),
        environ["CORE_DB_HOST"], environ.get("CORE_DB_PORT", "5432"),
        quote(environ.get("CORE_DB_NAME", "ncecore"), safe=""), options)
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
    if action == "bootstrap":
        return [".venv/bin/python", "deployment/aws_bootstrap.py", *arguments[1:]]
    if action == "idle":
        return [".venv/bin/python", "-c", "import time; time.sleep(86400)"]
    raise ValueError("Expected web, worker, idle, bootstrap or manage <management command>")


if __name__ == "__main__":
    try:
        configure()
        selected = command(sys.argv[1:])
        if not sys.argv[1:] or sys.argv[1] == "web":
            import subprocess
            subprocess.run([".venv/bin/python", "manage.py", "collectstatic", "--noinput"], check=True)
        os.execv(selected[0], selected)
    except (ValueError, KeyError) as error:
        # Variable names only; never print the environment or secret values.
        sys.exit(str(error))
