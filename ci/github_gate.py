#!/usr/bin/env python3
"""Verify exact-commit GitHub Actions checks before an operator release."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from platform_services.github_ci import main


if __name__ == '__main__':
    sys.exit(main())
