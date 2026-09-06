"""Fail a release whose tag disagrees with the version in the source.

The version lives in two files, so it is easy to bump one and forget the
other and ship binaries whose --version reports the wrong number. Run this
before tagging:

    python3 packaging/check_version.py v0.2.1

With no argument it reads the tag from GITHUB_REF_NAME and does nothing when
the ref is not a v* tag, which lets the same job run on ordinary pushes.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        return str(tomllib.loads(text)["project"]["version"])
    except ImportError:
        # Python 3.9/3.10 have no tomllib; the field is a plain literal.
        match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
        if not match:
            raise SystemExit("could not find version in pyproject.toml")
        return match.group(1)


def package_version() -> str:
    text = (ROOT / "csprpc" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("could not find __version__ in csprpc/__init__.py")
    return match.group(1)


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_REF_NAME", "")

    if not re.fullmatch(r"v\d+\.\d+\.\d+.*", tag):
        print("ref {!r} is not a version tag; nothing to check".format(tag))
        return 0

    wanted = tag[1:]
    found = {"pyproject.toml": pyproject_version(), "csprpc/__init__.py": package_version()}

    mismatched = {name: value for name, value in found.items() if value != wanted}
    if mismatched:
        print("tag {} expects version {!r}, but:".format(tag, wanted))
        for name, value in sorted(mismatched.items()):
            print("  {} says {!r}".format(name, value))
        print()
        print("Bump both files to {} and move the tag, or tag the right version.".format(wanted))
        return 1

    print("tag {} matches {} in both files".format(tag, wanted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
