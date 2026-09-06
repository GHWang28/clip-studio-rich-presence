"""Write a PyInstaller --version-file for the Windows exe.

Unsigned onefile builds look like packers to Defender. A real company name,
product name and version in the PE resources is one of the few free signals
that this is an application, not a dropper.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PACKAGING = Path(__file__).resolve().parent
if str(_PACKAGING) not in sys.path:
    sys.path.insert(0, str(_PACKAGING))

from check_version import package_version, pyproject_version  # noqa: E402

ROOT = _PACKAGING.parent

TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={filevers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'csprpc'),
        StringStruct('FileDescription', 'Discord Rich Presence for CLIP STUDIO PAINT'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'csprpc'),
        StringStruct('LegalCopyright', 'https://github.com/GHWang28/clip-studio-rich-presence'),
        StringStruct('OriginalFilename', 'csprpc.exe'),
        StringStruct('ProductName', 'CLIP STUDIO PAINT Rich Presence'),
        StringStruct('ProductVersion', '{version}')])
      ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def version_tuple(version: str) -> tuple:
    """Turn ``0.3.2`` or ``0.3.2rc1`` into the four-integer PE version."""
    core = version.split("+", 1)[0]
    for sep in ("a", "b", "rc"):
        index = core.find(sep)
        if index != -1:
            core = core[:index]
            break
    parts = [int(piece) for piece in core.split(".") if piece.isdecimal()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def render(version: str) -> str:
    return TEMPLATE.format(version=version, filevers=version_tuple(version))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        default=str(ROOT / "packaging" / "_file_version_info.txt"),
    )
    args = parser.parse_args(argv)

    project = pyproject_version()
    package = package_version()
    if project != package:
        raise SystemExit(
            "pyproject.toml is {!r} but csprpc/__init__.py is {!r}".format(project, package)
        )

    path = Path(args.output)
    path.write_text(render(project), encoding="utf-8")
    print("wrote {} for {}".format(path, project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
