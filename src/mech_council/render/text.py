from __future__ import annotations

import re

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "trivial": 3}


def severity_key(severity: str) -> tuple[int, str]:
    return (SEVERITY_ORDER.get(str(severity).casefold(), 2), "")


def caption(agent_id: str, names: dict[str, str]) -> str:
    """The report's form: the name beside the id every record of the run is written with."""
    name = names.get(agent_id)
    return f"{name} ({agent_id})" if name else agent_id


def name_of(agent_id: str, names: dict[str, str]) -> str:
    """The console's form: the name alone, the id left to the records."""
    return names.get(agent_id, agent_id)


#: A seat's id inside prose a model wrote.
_AGENT_ID = re.compile(r"Agent\d+")


def spoken(text: str, names: dict[str, str]) -> str:
    """A model's sentence with names in place of the ids it was handed; only the human's screen gets this."""
    return _AGENT_ID.sub(lambda found: names.get(found.group(), found.group()), text or "")


def direction_label(token: str, names: dict[str, str]) -> str:
    """A direction in the words the human is shown: the seat's name, or the label of the merge it stands for."""
    from ..deliberation.state import MERGED

    label = token.partition(MERGED)[2].strip() if token.startswith(MERGED) else ""
    return f"the merge `{label}`" if label else name_of(token, names)


def wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    out: list[str] = []
    for paragraph in (text or "").splitlines() or [""]:
        if not paragraph.strip():
            out.append("")
            continue
        line = indent
        for word in paragraph.split():
            if len(line) + len(word) + 1 > width and line.strip():
                out.append(line.rstrip())
                line = indent
            line += word + " "
        out.append(line.rstrip())
    return out
