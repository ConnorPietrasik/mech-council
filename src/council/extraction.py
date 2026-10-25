"""Lenient extraction of structured verdicts from model prose.

Leniency is scoped to model output and goes nowhere near the config loader.  Case and separators are not
identity; only *different words* need the synonym tables, which are data here so a mode can add to them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .deliberation.state import ABSTAIN, AGREE, DISAGREE, NEEDS_HUMAN, STANCES


#: fold case and drop `_`/`-`: `validationGate`/`VALIDATION_GATE` → `validation_gate`.
def fold(name: object) -> str:
    return re.sub(r"[_\-\s]+", "", str(name).casefold())


SYNONYMS: dict[str, tuple[str, ...]] = {
    "claim": ("claim", "title", "description", "finding", "summary", "issue", "problem"),
    "detail": ("detail", "body", "explanation", "context", "notes"),
    "evidence": ("evidence", "proof", "basis", "citation", "citations", "references"),
    "severity": ("severity", "impact", "priority", "importance"),
    "id": ("id", "item", "finding", "item_id", "finding_id", "topic", "topic_id", "ref", "key"),
    "stance": ("stance", "position", "vote", "opinion", "call", "assessment"),
    "note": ("note", "comment", "reason", "why", "justification", "remark"),
    "severity_": (),
    "positions": ("positions", "stances", "verdicts", "items", "calls", "reviews"),
    "new_items": ("new_items", "new_findings", "findings", "new", "additions", "new_topics", "topics"),
    "answers": ("answers", "replies", "responses", "qa_answers", "q_a"),
    "questions": ("questions", "agent_questions", "asks", "queries"),
    "human_input": ("human_input", "human", "needs_human", "questions_for_human", "human_questions", "escalations"),
    "signoff": ("signoff", "sign_off", "signed_off", "approve", "approval", "done"),
    "summary": ("summary", "overview", "wrap_up"),
    "open_questions": ("open_questions", "questions_open", "unresolved", "queries"),
    "revised_plan": ("revised_plan", "plan", "revision", "updated_plan", "the_plan"),
    "backing": ("backing", "back", "support", "backs", "vote_for", "choose", "pick"),
    "stages": ("stages", "steps", "phases"),
    "gate": ("gate", "validation_gate", "verify", "validation", "check", "test"),
    "state": ("state", "final_state", "end_state", "outcome", "result"),
    "exceptions": ("exceptions", "traps", "pitfalls", "caveats", "warnings"),
    "constraints": ("constraints", "constraints_across_stages", "cross_stage"),
    "risks": ("risks", "risk", "danger"),
    "assumptions": ("assumptions", "assumption"),
    "rejected_alternatives": ("rejected_alternatives", "alternatives", "rejected", "alternatives_rejected"),
    "agreed": ("agreed", "agreed_points", "common", "consensus_points"),
    "conflicts": ("conflicts", "disagreements", "conflict"),
    "uncovered": ("uncovered", "gaps", "missing", "coverage_gaps", "not_covered"),
    "groups": ("groups", "merges", "merge", "duplicates", "clusters"),
    "canonical": ("canonical", "canonical_id", "keep", "target", "into", "primary"),
    "duplicates": ("duplicates", "duplicate_ids", "dupes", "merged", "absorbed", "also"),
    "decisions": ("decisions", "decisions_list", "verdicts", "actions"),
    "action": ("action", "decision", "choice", "verdict"),
    "agent": ("agent", "agent_id", "seat", "who", "target", "member"),
    "restart": ("restart", "should_restart", "should_restart_agent"),
    "keep": ("keep", "should_keep"),
    "reason": ("reason", "why", "note", "explanation"),
    "to": ("to", "addressee", "target", "addressed_to", "whom"),
    "question": ("question", "text", "ask", "query", "what"),
    "item": ("item", "item_id", "finding", "finding_id", "topic", "topic_id", "on"),
    "blocking": ("blocking", "block", "is_blocking", "urgent"),
    "options": ("options", "choices", "alternatives"),
    "context": ("context", "background"),
    "approach": ("approach", "plan", "proposal"),
}

#: keys are folded at import, so `ask_human` and `ASK-HUMAN` reach the same row.
STANCE_WORDS: dict[str, str] = {
    "agree": AGREE,
    "agreed": AGREE,
    "yes": AGREE,
    "confirm": AGREE,
    "confirmed": AGREE,
    "support": AGREE,
    "valid": AGREE,
    "correct": AGREE,
    "accept": AGREE,
    "disagree": DISAGREE,
    "disagreed": DISAGREE,
    "no": DISAGREE,
    "reject": DISAGREE,
    "refuted": DISAGREE,
    "object": DISAGREE,
    "objection": DISAGREE,
    "false": DISAGREE,
    "wrong": DISAGREE,
    "contradict": DISAGREE,
    "needs_human": NEEDS_HUMAN,
    "human": NEEDS_HUMAN,
    "ask_human": NEEDS_HUMAN,
    "ask_user": NEEDS_HUMAN,
    "ask_the_user": NEEDS_HUMAN,
    "needs_clarification": NEEDS_HUMAN,
    "needshuman": NEEDS_HUMAN,
    "escalate": NEEDS_HUMAN,
    "blocked": NEEDS_HUMAN,
    "abstain": ABSTAIN,
    "neutral": ABSTAIN,
    "skip": ABSTAIN,
    "none": ABSTAIN,
    "no_opinion": ABSTAIN,
    "noposition": ABSTAIN,
    "withdaw": ABSTAIN,
    "withdraw": ABSTAIN,
    "withdraw_human": ABSTAIN,
    "no_human_needed": ABSTAIN,
    "drop_escalation": ABSTAIN,
    "no_longer_needs_human": ABSTAIN,
    "declined": ABSTAIN,
}
STANCE_WORDS = {fold(word): value for word, value in STANCE_WORDS.items()}

#: Lenient extraction's backing paraphrase table: three literals and nowhere else.
BACKING_WORDS: dict[str, str] = {
    "defer": "no_preference",
    "either": "no_preference",
    "any": "no_preference",
    "none": "no_preference",
    "whatever": "no_preference",
    "no_preference": "no_preference",
    "nopreference": "no_preference",
    "noweakpreferences": "no_preference",
    "no": "no_preference",
    "neutral": "no_preference",
}
BACKING_WORDS = {fold(word): value for word, value in BACKING_WORDS.items()}

WITHDRAW_WORDS = ("withdraw", "withdraw_human", "no_human_needed", "drop_escalation")

SEVERITIES = ("blocker", "major", "minor", "trivial")


@dataclass
class Extraction:
    """One parsed message plus the classification the cause table and lenient extraction need."""

    data: dict = field(default_factory=dict)
    repaired: bool = False
    errors: list[str] = field(default_factory=list)  # repairable problems
    invalid: bool = False  # no JSON at all / not an object
    summary: str = ""


def find_json(text: str) -> tuple[dict | None, str | None]:
    """The first balanced JSON object in the text, tolerating fences and prose.

    Tries every `{` until one parses — a model that opens with prose still lands.
    """
    if not text:
        return None, "empty answer"
    cleaned = text.replace("```json", "```")
    start = cleaned.find("{")
    last_error: str | None = None
    while start >= 0:
        end = _balanced_end(cleaned, start)
        if end is None:
            return None, "unbalanced JSON object in the answer"
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            last_error = f"unparseable JSON: {exc}"
        else:
            if isinstance(value, dict):
                return value, None
            last_error = "the answer's JSON is not an object"
        start = cleaned.find("{", start + 1)
    return None, last_error or "no JSON object found in the answer"


def _balanced_end(text: str, start: int) -> int | None:
    """The offset of the `}` that closes the `{` at `start`, string- and escape-aware."""
    depth, in_string, escape = 0, False, False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos
    return None


def pick(mapping: object, canonical: str, extra: tuple[str, ...] = ()) -> object:
    """Fetch `canonical` from a dict through folding and the synonym table.

    `mapping` is `object` because every caller here holds whatever a member's JSON parsed to,
    and the guard below is what turns that into a dict or into no value at all.
    """
    if not isinstance(mapping, dict):
        return None
    forms = {fold(k): k for k in mapping}
    candidates = [fold(canonical), *(fold(e) for e in extra)]
    candidates += [fold(s) for s in SYNONYMS.get(canonical, ())]
    for candidate in candidates:
        if candidate in forms:
            return mapping[forms[candidate]]
    return None


def text_of(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(text_of(v) for v in value if v is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def map_stance(value: object) -> str | None:
    """A stance paraphrase is mapped, never dropped; an unknown one invalidates the entry."""
    word = fold(value)
    if word in STANCES:
        return word
    return STANCE_WORDS.get(word)


def map_backing(value: object, me: str) -> tuple[str | None, bool]:
    """`(token, invalidated)` — an unmappable token invalidates `backing` **only**.

    Never defaulted to `no_preference`: a value the parser invented is not
    a shrug. `abstain` arriving here is the invalid case, not a synonym.
    """
    word = fold(value)
    if not word:
        return None, True
    if word in ("abstain", "declined"):
        return None, True
    if word in BACKING_WORDS:
        return BACKING_WORDS[word], False
    if word in ("me", "myplan", "mine", "self", "myself"):
        return me, False
    compact = word.replace("agent", "").replace(":", "")
    if compact.isdigit():
        return f"Agent{int(compact)}", False
    if word.startswith(("merged", "merge")):
        label = text_of(value).split(":", 1)[-1].strip() if ":" in text_of(value) else ""
        label = label or text_of(value)[len("merged") :].strip()
        return (f"merged: {label}", False) if label else (None, True)
    return None, True


def map_severity(value: object) -> str:
    """Unknown severities degrade to `minor`."""
    word = fold(value)
    if word in ("blocker", "critical", "fatal", "severe", "high"):
        return "blocker"
    if word in ("major", "important", "medium", "med"):
        return "major"
    if word in ("trivial", "nit", "cosmetic", "style"):
        return "trivial"
    if word in SEVERITIES:
        return word
    return "minor"


def parse_verdict(
    payload: str,
    *,
    mode: str,
    me: str,
    extra_synonyms: dict | None = None,
    known_items: set[str] | None = None,
    closed_items: set[str] | None = None,
) -> Extraction:
    """One seat's round verdict: positions, new items, Q&A, human_input, signoff (+ plan)."""
    data, error = find_json(payload)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
    if extra_synonyms:
        for key, words in extra_synonyms.items():
            SYNONYMS.setdefault(key, ())
            SYNONYMS[key] = tuple(dict.fromkeys(tuple(SYNONYMS[key]) + tuple(words)))
    out: Extraction = Extraction(data={})
    known = known_items or set()
    closed = closed_items or set()
    unknown: list[str] = []

    positions = pick(data, "positions") or []
    if not isinstance(positions, list):
        out.errors.append("`positions` is not a list")
        positions = []
    clean_positions, dropped = [], 0
    for entry in positions:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        raw_id = text_of(pick(entry, "id")).strip()
        stance = map_stance(pick(entry, "stance"))
        if stance is None:
            dropped += 1  # an unknown stance invalidates the entry
            continue
        if not raw_id:
            dropped += 1
            continue
        if raw_id not in known:
            unknown.append(raw_id)  # a stance for an unknown item must not vanish
            continue
        item: dict[str, object] = {
            "item": raw_id,
            "stance": stance,
            "note": text_of(pick(entry, "note")),
            "evidence": text_of(pick(entry, "evidence")),
        }
        if raw_id in closed:
            item["on_closed"] = True
        clean_positions.append(item)
    if positions and dropped and not clean_positions:
        out.errors.append("`positions` was present but every entry was invalid")
    out.data["positions"] = clean_positions
    out.data["unknown_items"] = unknown
    out.data["dropped_positions"] = dropped

    new_items = pick(data, "new_items") or []
    if not isinstance(new_items, list):
        new_items = []
    clean_new = []
    for entry in new_items:
        if isinstance(entry, str):
            entry = {"claim": entry}
        if not isinstance(entry, dict):
            continue
        claim = text_of(pick(entry, "claim"))
        if not claim:
            continue
        clean_new.append(
            {
                "claim": claim,
                "detail": text_of(pick(entry, "detail")),
                "evidence": text_of(pick(entry, "evidence")),
                "severity": map_severity(pick(entry, "severity")),
            }
        )
    out.data["new_items"] = clean_new

    out.data["answers"] = _entries(pick(data, "answers"), ("to", "question"), text=("answer", "text", "reply"))
    out.data["questions"] = _entries(pick(data, "questions"), ("to",), text=("question", "text", "ask"))
    out.data["human_input"] = _human_input(data)
    signoff = pick(data, "signoff")
    out.data["signoff"] = bool(signoff) if signoff is not None else False
    out.data["summary"] = text_of(pick(data, "summary"))
    out.data["open_questions"] = _strings(pick(data, "open_questions"))
    if mode == "plan":
        revised = pick(data, "revised_plan")
        out.data["revised_plan"] = _revised_plan(revised)
        raw_backing = pick(data, "backing")
        if raw_backing is None:
            out.data["backing"] = None
        else:
            token, invalidated = map_backing(raw_backing, me)
            if invalidated:
                out.errors.append(f"`backing` {text_of(raw_backing)!r} is not one of AgentN / merged: <label> / no_preference")
            out.data["backing"] = token
    if out.errors and not clean_positions:
        out.repaired = True  # the caller owes the repair re-ask for this message
    return out


def _entries(value: object, need: tuple[str, ...], *, text: tuple[str, ...] = ("text",)) -> list[dict]:
    """`answers`/`questions` rows: the fields the engine routes on, plus the prose.

    The text key is passed in because the two schemas differ: an answer carries `answer`, a question carries `question` — and
    the `question` field of an *answer* is its `Q<n>` id, which must never be mistaken for the answer's text.
    """
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, dict):
            continue
        row = {key: text_of(pick(entry, key)) for key in need}
        for key in text:
            body = text_of(pick(entry, key))
            if body:
                row["text"] = body
                break
        else:
            row["text"] = ""
        out.append(row)
    return out


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [text_of(v) for v in value if text_of(v).strip()]


def _human_input(data: dict) -> list[dict]:
    """An entry naming an item is an escalation request, one naming none is a question."""
    raw = pick(data, "human_input")
    if isinstance(raw, str):
        raw = [{"question": raw}]
    if not isinstance(raw, list):
        raw = []
    out = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"question": entry}
        if not isinstance(entry, dict):
            continue
        question = text_of(pick(entry, "question")) or text_of(entry)
        if not question or question == "{}":
            continue
        blocking = pick(entry, "blocking")
        out.append(
            {
                "question": question,
                "item": text_of(pick(entry, "item")).strip() or None,
                "blocking": bool(blocking) if blocking is not None else False,
                "context": text_of(pick(entry, "context")),
                "options": _strings(pick(entry, "options")),
            }
        )
    return out


def _revised_plan(value: object) -> dict | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"name": "", "text": value, "stages": []}
    if not isinstance(value, dict):
        return None
    stages = pick(value, "stages") or []
    clean = []
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, str):
                clean.append({"state": stage, "gate": "", "scope": "", "exceptions": []})
            elif isinstance(stage, dict):
                clean.append(
                    {
                        "state": text_of(pick(stage, "state")),
                        "gate": text_of(pick(stage, "gate")),
                        "scope": text_of(pick(stage, "scope")),
                        "exceptions": _strings(pick(stage, "exceptions")),
                    }
                )
    return {"name": text_of(pick(value, "name")), "text": text_of(pick(value, "claim")) or text_of(value)[:0], "stages": clean}


def parse_phase0(payload: str, *, mode: str, extra_synonyms: dict | None = None) -> Extraction:
    """Phase 0: findings (review) or plans (plan), plus summary and open_questions."""
    data, error = find_json(payload)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
    out = Extraction(data=data)
    if mode == "plan":
        approach = pick(data, "approach") or data
        if isinstance(approach, str):
            approach = {"claim": approach}
        stages = pick(approach, "stages") if isinstance(approach, dict) else []
        clean_stages = []
        if isinstance(stages, list):
            for stage in stages:
                if isinstance(stage, dict):
                    clean_stages.append(
                        {
                            "state": text_of(pick(stage, "state")),
                            "gate": text_of(pick(stage, "gate")),
                            "scope": text_of(pick(stage, "scope")),
                            "exceptions": _strings(pick(stage, "exceptions")),
                        }
                    )
                elif isinstance(stage, str):
                    clean_stages.append({"state": stage, "gate": "", "scope": "", "exceptions": []})
        out.data["plan"] = {
            "text": text_of(pick(approach, "claim")) if isinstance(approach, dict) else "",
            "stages": clean_stages,
            "constraints": _strings(pick(approach, "constraints")),
            "risks": _strings(pick(approach, "risks")),
            "assumptions": _strings(pick(approach, "assumptions")),
            "rejected_alternatives": _strings(pick(approach, "rejected_alternatives")),
        }
        if not clean_stages and not out.data["plan"]["text"]:
            out.errors.append("the plan carries no stages")
            out.repaired = True
    else:
        findings = pick(data, "new_items") or []
        if not isinstance(findings, list):
            findings = []
        clean = []
        for entry in findings:
            if isinstance(entry, str):
                entry = {"claim": entry}
            if not isinstance(entry, dict):
                continue
            claim = text_of(pick(entry, "claim"))
            if not claim:
                continue
            clean.append(
                {
                    "claim": claim,
                    "detail": text_of(pick(entry, "detail")),
                    "evidence": text_of(pick(entry, "evidence")),
                    "severity": map_severity(pick(entry, "severity")),
                }
            )
        out.data["findings"] = clean
        if findings and not clean:
            out.errors.append("`findings` was present but no entry carried a claim")
            out.repaired = True
    out.data["summary"] = text_of(pick(data, "summary"))
    out.data["open_questions"] = _strings(pick(data, "open_questions"))
    # The phase-0 schema advertises `human_input` with `context` and `options`, so extraction reads them here too:
    # what the phase-0 brief asks for and the run then discards is a prompt that lies.
    out.data["human_input"] = _human_input(data)
    return out


def parse_merge_plan(payload: str) -> tuple[dict | None, list[dict], list[str]]:
    """The chair's merge plan: groups of duplicate IDs plus any questions it raises.

    Structural validation lives with the engine's `_apply_merge`, which both modes reach; this only extracts.
    """
    data, error = find_json(payload)
    if data is None:
        return None, [], [error or "no JSON object found"]
    groups, merges = pick(data, "groups"), pick(data, "merges")
    rows = groups if isinstance(groups, list) else merges if isinstance(merges, list) else []
    clean = []
    for group in rows:
        if not isinstance(group, dict):
            continue
        canonical = text_of(pick(group, "canonical")).strip()
        dups = pick(group, "duplicates")
        if isinstance(dups, str):
            dups = [d for d in re.split(r"[,\s]+", dups) if d]
        if not canonical or not isinstance(dups, list):
            continue
        clean.append({"canonical": canonical, "duplicates": [text_of(d).strip() for d in dups if text_of(d).strip()]})
    questions = _human_input(data)
    return {"groups": clean}, questions, []


def parse_comparison(payload: str) -> tuple[dict | None, list[str]]:
    """The plan comparison pass: agreed points, conflicts, uncovered, human_input."""
    data, error = find_json(payload)
    if data is None:
        return None, [error or "no JSON object found"]
    out = {
        "agreed": _agreed(pick(data, "agreed")),
        "conflicts": _conflicts(pick(data, "conflicts")),
        "uncovered": _strings(pick(data, "uncovered")),
        "human_input": _human_input(data),
        "summary": text_of(pick(data, "summary")),
    }
    return out, []


def _agreed(value: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if isinstance(entry, str):
            out.append({"point": entry, "agents": []})
        elif isinstance(entry, dict):
            agents = pick(entry, "agents") or pick(entry, "agent") or []
            out.append(
                {
                    "point": text_of(pick(entry, "point")) or text_of(pick(entry, "claim")) or text_of(entry),
                    "agents": _strings(agents) if isinstance(agents, list) else _strings([agents]),
                }
            )
    return out


def _conflicts(value: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, dict):
            continue
        positions = pick(entry, "positions") or []
        rows = []
        if isinstance(positions, list):
            for row in positions:
                if isinstance(row, dict):
                    # The plan phase machine's schema is `{"agent", "claim"}`; `position`/`note` are synonyms.
                    rows.append({"agent": text_of(pick(row, "agent")), "claim": text_of(pick(row, "claim")) or text_of(pick(row, "note"))})
        out.append({"claim": text_of(pick(entry, "claim")) or text_of(pick(entry, "question")), "positions": rows})
    return out


def parse_watchdog(payload: str) -> tuple[list[dict], list[dict], list[str]]:
    """The chair's watchdog decisions: per-agent keep/restart plus any questions."""
    data, error = find_json(payload)
    if data is None:
        return [], [], [error or "no JSON object found"]
    decisions = pick(data, "decisions")
    if not isinstance(decisions, list):
        return [], [], ["the watchdog reply carried no `decisions` array"]
    out = []
    for entry in decisions:
        if not isinstance(entry, dict):
            continue
        agent = text_of(pick(entry, "agent")).strip()
        action = fold(pick(entry, "action"))
        if action in ("restart", "keep"):
            pass
        elif pick(entry, "restart") is not None:
            action = "restart" if pick(entry, "restart") else "keep"
        elif pick(entry, "keep") is not None:
            action = "keep" if pick(entry, "keep") else "restart"
        else:
            continue
        out.append({"agent": agent, "action": action, "reason": text_of(pick(entry, "reason"))})
    return out, _human_input(data), []


def schema_hint(mode: str, *, phase: str) -> str:
    """Lenient extraction's repair re-ask carries the schema hint; the text lives in `prompts/`."""
    from .prompts import common

    return common.schema_hint(mode, phase=phase)


def extract(kind: str, text: str, **kwargs) -> Extraction:
    """One dispatch over the parsers, so a lane repairs whatever kind of message it sent."""
    if kind == "phase0":
        return parse_phase0(text, **kwargs)
    if kind == "verdict":
        return parse_verdict(text, **kwargs)
    if kind == "merge":
        plan, questions, errors = parse_merge_plan(text)
        return Extraction(data={"groups": (plan or {}).get("groups", []), "questions": questions}, invalid=plan is None, errors=errors)
    if kind == "compare":
        comparison, errors = parse_comparison(text)
        return Extraction(data=comparison or {}, invalid=comparison is None, errors=errors)
    if kind == "watchdog":
        decisions, questions, errors = parse_watchdog(text)
        return Extraction(data={"decisions": decisions, "questions": questions}, invalid=not decisions, errors=errors)
    raise KeyError(kind)
