from __future__ import annotations

import sys

from .cli import main as _main


def main(argv: list[str] | None = None) -> int:
    code = _main(argv)
    sys.stdout.flush()
    sys.stderr.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
