from __future__ import annotations


def normalize(name: str) -> str:
    return name.strip().lstrip("-").casefold().replace("_", "").replace("-", "")


def distance(a: str, b: str) -> int:
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
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),
                )
            )  # substitution
        previous = current
    return previous[-1]


def threshold(form: str) -> int:
    return 2 if len(form) >= 6 else 1


def _closest(forms: list[str], probe: str) -> str:
    best, best_d = forms[0], distance(probe, forms[0])
    for form in forms[1:]:
        d = distance(probe, form)
        if d < best_d:
            best, best_d = form, d
    return best


def suggest(refused: str, known: list[str] | tuple[str, ...]) -> str | None:
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
        # A prefix is a weak signal when the refused name is a *near-miss* of something longer: `review-stagwd` is a prefix of
        # nothing but one edit from `review-staged`, and a hint that sends someone to the wrong council is worse than no hint.
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
