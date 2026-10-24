"""§3.1/§3.3's one suggestion function — flags, config keys and council names alike.

Reached only for a name the parser has already refused, so there is no "it matched,
so stay silent" case: a normalized match is the hint's best answer, never a reason to
withhold one.  It **suggests, never accepts** (§3.3).

The ladder (plan D8), after normalizing both sides:

1. exact normalized match → that name;
2. prefix match either way round → that name, resolved against step 3 whenever a
   near-miss is *closer* than the prefix: a hint that names a real but wrong council is
   worse than no hint, because convening it is not harmless;
3. Levenshtein over the normalized forms: ≤1 edit under 6 chars, ≤2 at 6 and above.

No match at any step → no hint at all: §3.1's restraint is the point.
"""
from __future__ import annotations


def normalize(name: str) -> str:
    """Case folded, ``_``/``-`` dropped, leading dashes stripped."""
    return name.strip().lstrip("-").casefold().replace("_", "").replace("-", "")


def distance(a: str, b: str) -> int:
    """Plain Levenshtein over two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1,          # deletion
                               current[j - 1] + 1,       # insertion
                               previous[j - 1] + (ca != cb)))  # substitution
        previous = current
    return previous[-1]


def threshold(form: str) -> int:
    """≤1 edit for short tokens, ≤2 for ≥6 chars (§3.1)."""
    return 2 if len(form) >= 6 else 1


def _closest(forms: list[str], probe: str) -> str | None:
    best, best_d = None, None
    for form in forms:
        d = distance(probe, form)
        if best_d is None or d < best_d:
            best, best_d = form, d
    return best


def suggest(refused: str, known: list[str] | tuple[str, ...]) -> str | None:
    """The canonical spelling of the closest known name, or ``None``.

    Two *known* names that normalize alike both print, joined by ``/``, rather than
    one being picked (possible for council names, which are user text; not for the
    fixed flag and key sets).
    """
    probe = normalize(refused)
    if not probe:
        return None
    by_form: dict[str, list[str]] = {}
    for name in known:
        by_form.setdefault(normalize(name), []).append(name)

    def render(names: list[str]) -> str:
        return "/".join(dict.fromkeys(names))

    # 1. exact normalized match.
    if probe in by_form:
        return render(by_form[probe])

    # 2. prefix match, either way round.
    hits = [form for form in by_form if form.startswith(probe) or probe.startswith(form)]
    if hits:
        near = [form for form in by_form if distance(probe, form) <= threshold(probe)]
        pick = _closest(hits, probe)
        # A prefix is a weak signal when the refused name is a *near-miss* of something
        # longer: `review-stagwd` is a prefix of nothing but one edit from `review-staged`,
        # and a hint that sends someone to the wrong council is worse than no hint.
        if near:
            best = _closest(near, probe)
            if distance(probe, best) < distance(probe, pick):
                pick = best
        return render(by_form[pick])

    # 3. bounded edit distance.
    near = [form for form in by_form if distance(probe, form) <= threshold(probe)]
    if near:
        return render(by_form[_closest(near, probe)])
    return None
