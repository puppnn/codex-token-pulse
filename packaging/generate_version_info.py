from __future__ import annotations

import argparse
from pathlib import Path


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.lstrip("v").split(".")
    values = [int(part) for part in parts[:4]]
    values.extend([0] * (4 - len(values)))
    return tuple(values)  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    version = args.version.lstrip("v")
    numeric = version_tuple(version)
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404B0',
        [
          StringStruct(u'CompanyName', u'puppnn'),
          StringStruct(u'FileDescription', u'Token Pulse'),
          StringStruct(u'FileVersion', u'{version}'),
          StringStruct(u'InternalName', u'TokenPulse'),
          StringStruct(u'LegalCopyright', u'Copyright (c) puppnn'),
          StringStruct(u'OriginalFilename', u'TokenPulse.exe'),
          StringStruct(u'ProductName', u'Token Pulse'),
          StringStruct(u'ProductVersion', u'{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
