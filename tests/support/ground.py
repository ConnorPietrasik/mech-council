"""§13.9's ground rule, in one place: where fixtures may be built, and when the
sandbox family must say it cannot run rather than pass on a filesystem that cannot hold
the layers §10 mounts.

`COUNCIL_TEST_FS` names plain ground when the operator knows where it is.  `$TMPDIR` is
right inside a council member — there it is that member's own scratch — and wrong outside
one when `/tmp` is a container root's overlay, which is the common case.  The checkout's
own filesystem is the last resort.  The family then decides honestly from the fstype
instead of mounting one layer on an overlay and calling the result green.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

FIXTURE_ROOT = os.path.realpath(os.environ.get("COUNCIL_TEST_FS")
                                or os.environ.get("TMPDIR")
                                or os.path.join(_ROOT, ".council-test-fs"))


def fstype_under(path: str) -> str:
    """The fstype of the deepest mountinfo prefix of `path`.

    mountinfo reads `… mount_point opts - fstype source superopts`, so the fstype is
    *after* the separator; reading the wrong half disables the skip in silence.
    """
    best, fstype = "", "unknown"
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for line in handle:
                if " - " not in line:
                    continue
                left, right = line.rstrip("\n").split(" - ", 1)
                fields = left.split(" ")
                if len(fields) < 5:
                    continue
                point = fields[4]
                if (path == point or path.startswith(point.rstrip("/") + "/")) \
                        and len(point) > len(best):
                    best, fstype = point, right.split(" ")[0]
    except OSError:
        pass
    return fstype


#: The literals a suite-wide "no silent skips" check looks for (§13.9).  They live here so
#: the checking test file does not contain them and match itself.
SKIP_MARKERS = ("pytest.skip", "skipif")
GROUND_SKIP_PHRASE = "lower inside an overlay"
