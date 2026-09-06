"""Entry point for PyInstaller bundles.

A dedicated script rather than ``csprpc/__main__.py``: PyInstaller treats the
script it is handed as a top-level module, which would shadow the package.
"""

import sys

from csprpc.cli import main

if __name__ == "__main__":
    sys.exit(main())
