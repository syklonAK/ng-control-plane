"""Allow running as ``python -m pg_router``."""

import sys

from .cli.app import main

if __name__ == "__main__":
    sys.exit(main())
