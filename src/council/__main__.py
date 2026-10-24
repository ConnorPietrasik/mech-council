"""`python -m council` — the module entry, and §11.0's flush-on-every-exit.

The body is `cli.main()`: `council`'s console script and this module must not be two
programs.  The only thing added here is what a module entry owes that a script does not:
stdout is flushed on the way out, because §11.0 says the `[council]` lines are the story
of the run and a buffered tail would lose the last chapter.
"""
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
