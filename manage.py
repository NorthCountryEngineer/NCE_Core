#!/usr/bin/env python3
import os
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"dev", "--local"}:
        from scripts.local_dev import LocalConfigurationError, configure_local, serve
        try:
            if sys.argv[1] == "dev":
                serve(sys.argv[2:])
                sys.exit(0)
            sys.argv.pop(1)
            if len(sys.argv) > 1 and sys.argv[1] == "runserver":
                raise LocalConfigurationError("Start the local server with: python manage.py dev [--port PORT]")
            configure_local()
        except LocalConfigurationError as error:
            sys.exit(str(error))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
