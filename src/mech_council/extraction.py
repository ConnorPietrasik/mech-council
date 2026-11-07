from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .deliberation.notes import NOTE_MAX_CHARS, NOTE_MAX_PER_REPLY
from .deliberation.state import ABSTAIN, AGREE, CHAIR_ID, DECLINED, DISAGREE, MERGED, NEEDS_HUMAN, NO_PREFERENCE, STANCES


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
    "signoff": ("signoff", "sign_off", "signed_off", "approved", "approve", "approval", "done"),
    "summary": ("summary", "overview", "wrap_up"),
    "open_questions": ("open_questions", "questions_open", "unresolved", "queries"),
    "notes": ("notes", "note", "human_notes", "notes_for_human", "human_note", "note_to_human", "remarks", "observations"),
    "retract_note": (
        "retract_note",
        "retract_notes",
        "drop_note",
        "drop_notes",
        "withdraw_note",
        "withdrawn_notes",
        "retracted_notes",
    ),
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
    "how": ("how", "how_addressed", "what_you_did", "response"),
    "say_to": ("say_to", "say_to_message", "messages", "say"),
    "changed": ("changed", "changed_files", "files_changed", "touched", "files"),
    "user_text": ("user_text", "user_visible_text", "text_surfaces", "user_facing", "user_facing_text"),
    "gate_command": ("gate_command", "gate_cmd", "command", "check_command", "gate"),
    "gate_output": ("gate_output", "output", "gate_result", "result"),
    "open_issues": ("open_issues", "issues", "open_issue"),
    "addressed": ("addressed", "fixes", "handled", "fixes_for"),
    "kind": ("kind", "role", "type"),
    "blocking": ("blocking", "block", "is_blocking", "urgent"),
    "options": ("options", "choices", "alternatives"),
    "context": ("context", "background"),
    "second": ("second", "seconds", "second_question", "backs_question", "agree_with_question", "seconded"),
    "blocking_for": ("blocking_for", "block_for", "blocking", "stop_for", "wait_for", "blocks"),
    "retract": ("retract", "withdraw", "retract_question", "withdraw_question", "drop_question", "no_longer_needed"),
    "answer_question": ("answer_question", "answer_human_question", "answers_for_human", "human_answers", "answer_ask"),
    "insist": ("insist", "insisted", "insist_on", "not_enough", "answer_not_enough", "reject_answer"),
    "approach": ("approach", "plan", "proposal"),
}

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

#: a paraphrase → the canonical backing token
BACKING_WORDS: dict[str, str] = {
    "defer": NO_PREFERENCE,
    "either": NO_PREFERENCE,
    "any": NO_PREFERENCE,
    "none": NO_PREFERENCE,
    "whatever": NO_PREFERENCE,
    "no_preference": NO_PREFERENCE,
    "nopreference": NO_PREFERENCE,
    "noweakpreferences": NO_PREFERENCE,
    "no": NO_PREFERENCE,
    "neutral": NO_PREFERENCE,
}
BACKING_WORDS = {fold(word): value for word, value in BACKING_WORDS.items()}


SEVERITIES = ("blocker", "major", "minor", "trivial")


#: The fields each pass is told it may send (the `prompts/*_SCHEMA` blocks); an object carrying none of them is
#: prose, not the answer.
VERDICT_FIELDS = (
    "summary",
    "positions",
    "new_items",
    "gate",
    "questions",
    "answers",
    "human_input",
    "second",
    "blocking_for",
    "retract",
    "insist",
    "answer_question",
    "open_questions",
    "notes",
    "retract_note",
    "signoff",
    "backing",
    "revised_plan",
    "approach",
)
#: phase 0 in either mode: `approach` may be the whole reply, or the reply itself
PHASE0_FIELDS = (
    "summary",
    "new_items",
    "approach",
    "claim",
    "stages",
    "constraints",
    "risks",
    "assumptions",
    "rejected_alternatives",
    "open_questions",
    "notes",
    "human_input",
)
MERGE_FIELDS = ("groups",)
DIRECTION_FIELDS = ("question", "context")
#: the chair's gate verification: the verdict, and what it saw
GATE_CHECK_FIELDS = ("ok", "ran", "evidence")
COMPARISON_FIELDS = ("agreed", "conflicts", "uncovered", "summary")
WATCHDOG_FIELDS = ("decisions",)
#: the builder's work and fix replies: the stage's result, as the orchestrator re-checks it
WORK_FIELDS = (
    "summary",
    "gate_command",
    "gate_output",
    "changed",
    "user_text",
    "open_issues",
    "human_input",
    "notes",
    "say_to",
    "addressed",
)
#: the builder's replan reply: a revised, smaller breakdown of the stuck stage
REPLAN_FIELDS = ("note", "stages")
#: the run's chair structuring and classification replies: stages with gates and kinds
STRUCTURE_FIELDS = ("stages",)


@dataclass
class Extraction:
    data: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # the reply itself is unreadable: the whole message goes through a repair re-ask
    refused: list[dict[str, str]] = field(default_factory=list)  # a field that will not be honored: read at its safe value, never guessed
    invalid: bool = False  # no JSON object at all

    @property
    def problems(self) -> list[str]:
        """Every sentence for the seat: what to repair, and what was refused and how it was read."""
        out = list(self.errors)
        for r in self.refused:
            named = f"`{r['field']}`" + (f" on {r['about']!r}" if r.get("about") else "")
            out.append(f"{named} {r['why']}, not {r['said']!r}: it counts as {r['counts']}")
        return out


def find_json(text: str, fields: tuple[str, ...] = ()) -> tuple[dict | None, str | None]:
    """The reply's answer object: the first complete object that carries one of `fields`, ignoring prose's own.
    With no `fields`, the first object still wins.
    """
    if not text:
        return None, "empty answer"
    cleaned = text.replace("```json", "```")
    unrelated = False  # an object was there; it just was not the answer
    last_error: str | None = None
    start = cleaned.find("{")
    while start >= 0:
        end = _balanced_end(cleaned, start)
        if end is None:
            # an unclosed object is a reply cut off mid-answer; its contents are a fragment
            if _opens_an_object(cleaned, start):
                return None, "unbalanced JSON object in the answer: the reply was cut off, so send the object whole"
            start = cleaned.find("{", start + 1)
            continue
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            last_error = f"unparseable JSON: {exc}"
            start = cleaned.find("{", start + 1)
            continue
        if not isinstance(value, dict):
            last_error = "the answer's JSON is not an object"
            start = cleaned.find("{", start + 1)
            continue
        if not fields or _carries(value, fields):
            return value, None
        unrelated = True  # a quoted object's contents are quoted too: a nested object is not the answer
        start = cleaned.find("{", end + 1)
    if unrelated:
        return None, "no answer object in the reply: its JSON carries none of the fields the schema names"
    return None, last_error or "no JSON object found in the answer"


def _carries(data: dict, fields: tuple[str, ...]) -> bool:
    """Whether the object is the payload: any of its fields, under any name the seat sent it with."""
    return any(pick(data, name) is not None for name in fields)


def _opens_an_object(text: str, start: int) -> bool:
    pos = start + 1
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos < len(text) and text[pos] == '"'


def _balanced_end(text: str, start: int) -> int | None:
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
    if not isinstance(mapping, dict):
        return None
    forms = {fold(k): k for k in mapping}
    candidates = [fold(canonical), *(fold(e) for e in extra)]
    candidates += [fold(s) for s in SYNONYMS.get(canonical, ())]
    for candidate in candidates:
        if candidate in forms:
            return mapping[forms[candidate]]
    return None


#: a boolean a seat sent as a word instead of as JSON: the bare words it means by no, and by yes, in `fold`'s form
NO_TOKENS = frozenset({"false", "no", "nope", "nah", "n", "none", "null", "nil", "off", "never", "negative", "denied", "declined", "0"})
YES_TOKENS = frozenset({"true", "yes", "yeah", "yep", "y", "on", "ok", "okay", "affirmative", "1"})
#: a sign-off is the flag a seat words as its verdict
SIGNOFF_YES = frozenset({"approved", "approve", "agreed", "agree", "accepted", "signedoff", "lgtm"})


def _flag(value: object, field: str, refused: list[dict[str, str]], *, about: str = "", yes: frozenset[str] = frozenset()) -> bool:
    """A flag is a bool, or one of those words on its own; a word about anything else counts false and is refused."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, dict)) and not value:
        return False  # an empty container carries no word at all: it means no
    word = fold(text_of(value)).rstrip(".!?,;:")
    if not word or word in NO_TOKENS:
        return False
    if word in YES_TOKENS or word in yes:
        return True
    entry = {"field": field, "said": text_of(value).strip(), "why": "must be true or false", "counts": "false"}
    if about:
        entry["about"] = about
    refused.append(entry)
    return False


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
    word = fold(value)
    if word in STANCES:
        return word
    return STANCE_WORDS.get(word)


def map_backing(value: object, me: str) -> tuple[str | None, bool]:
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
        return (f"{MERGED} {label}", False) if label else (None, True)
    return None, True


def map_severity(value: object) -> str:
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


def _gate_verdict(value: object, refused: list[dict[str, str]]) -> dict | None:
    """A seat's verdict on a stage's gate: {ok, ran, evidence}, or None when it reported none."""
    if value is None:
        return None
    if isinstance(value, dict):
        if pick(value, "ok") is None:
            refused.append({"field": "gate", "why": "must carry an ok verdict", "counts": "not counted"})
            return None
        return {
            "ok": _flag(pick(value, "ok"), "gate.ok", refused),
            "ran": text_of(pick(value, "ran")),
            "evidence": text_of(pick(value, "evidence")),
        }
    word = fold(text_of(value))
    if word.startswith("pass") or word in YES_TOKENS:
        ok = True
    elif word.startswith("fail") or word in NO_TOKENS:
        ok = False
    else:
        refused.append(
            {"field": "gate", "said": text_of(value).strip(), "why": "must say whether the gate passed", "counts": "not counted"}
        )
        return None
    return {"ok": ok, "ran": "", "evidence": text_of(value).strip()}


def parse_verdict(
    payload: str,
    *,
    mode: str,
    me: str,
    known_items: set[str] | None = None,
    closed_items: set[str] | None = None,
) -> Extraction:
    data, error = find_json(payload, VERDICT_FIELDS)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
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
            dropped += 1
            continue
        if not raw_id:
            dropped += 1
            continue
        if raw_id not in known:
            unknown.append(raw_id)
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
    out.data["gate"] = _gate_verdict(pick(data, "gate"), out.refused)

    out.data["answers"] = _entries(pick(data, "answers"), ("to", "question"), text=("answer", "text", "reply"))
    out.data["questions"] = _entries(pick(data, "questions"), ("to",), text=("question", "text", "ask"))
    out.data["human_input"] = _human_input(data, out.refused)
    out.data["second"] = _ids(pick(data, "second"))
    out.data["blocking_for"] = _ids(pick(data, "blocking_for"))
    out.data["retract"] = _ids(pick(data, "retract"))
    out.data["answer_question"] = _ids_entries(pick(data, "answer_question"))
    out.data["insist"] = _ids(pick(data, "insist"))
    out.data["signoff"] = _flag(pick(data, "signoff"), "signoff", out.refused, yes=SIGNOFF_YES)
    out.data["summary"] = text_of(pick(data, "summary"))
    out.data["open_questions"] = _texts(pick(data, "open_questions"))
    out.data["notes"] = _notes(pick(data, "notes"))
    out.data["retract_note"] = _retract_note(pick(data, "retract_note"))
    out.data["say_to"] = _say_to(pick(data, "say_to"), out.refused)
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
    return out


def _entries(value: object, need: tuple[str, ...], *, text: tuple[str, ...] = ("text",)) -> list[dict]:
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


def _notes(value: object) -> list[dict]:
    """`notes` lines, written as strings or as `{"note": …, "item": "F2"}` objects. Junk is dropped, never an error."""
    raw = [value] if isinstance(value, (dict, str)) else value if isinstance(value, list) else []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"note": entry}
        if not isinstance(entry, dict):
            continue
        text = text_of(pick(entry, "note", ("text", "message", "remark", "observation", "human_note", "content", "value")))
        text = text.strip()
        if not text:
            continue
        item = text_of(pick(entry, "item")).strip() or None
        out.append({"text": text[:NOTE_MAX_CHARS], "item": item})
    return out[:NOTE_MAX_PER_REPLY]


def _retract_note(value: object) -> list[dict]:
    """`retract_note` lines: `["N1"]`, or `[{"id": "N1", "reason": "what was wrong"}]`.

    A bare id retracts; the reason is what the human is told.
    """
    raw = [value] if isinstance(value, (dict, str)) else value if isinstance(value, list) else []
    out: list[dict] = []
    for entry in raw:
        if isinstance(entry, (str, int, float)):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        said = ""
        for key in [k for k in entry if fold(k) == "note"]:
            # `note` is a synonym of both `id` and `reason`: an id names the note, prose is the reason
            under_note = entry.pop(key)
            named = _ids(under_note)
            if named and _NOTE_ID.fullmatch(named[0]):
                entry.setdefault("id", named[0])
            else:
                said = text_of(under_note).strip()
        token = _ids(pick(entry, "id", ("note_id", "nid", "n")))
        if not token or any(one["id"] == token[0] for one in out):
            continue
        reason = text_of(pick(entry, "reason")).strip() or said or text_of(pick(entry, "note")).strip()
        out.append({"id": token[0], "reason": reason[:NOTE_MAX_CHARS]})
    return out


def _texts(value: object) -> list[str]:
    """`open_questions` lines, whether a seat wrote strings or `{"question": …}` objects."""
    raw = [value] if isinstance(value, dict) else value if isinstance(value, list) else [value] if isinstance(value, str) else []
    out = []
    for entry in raw:
        text = text_of(pick(entry, "question")) if isinstance(entry, dict) else text_of(entry)
        if text.strip() and text not in out:
            out.append(text)
    return out


#: an ask id and nothing else
_ASK_ID = re.compile(r"H\d+")

#: a note id and nothing else
_NOTE_ID = re.compile(r"N\d+")

#: a docket item id, any mode's prefix: what `addressed` may name
_ITEM_ID = re.compile(r"[A-Z]\d+")

#: a seat id, as `say_to` names it
_SEAT_ID = re.compile(r"Agent\d+")


def _ids(value: object) -> list[str]:
    """Docket ids as a seat typed them: `h1`, `#H1` and ` H1 ` all name H1."""
    out = []
    for entry in _strings(value):
        token = entry.strip().lstrip("#").upper().replace(" ", "")[:12]
        if token and token not in out:
            out.append(token)
    return out


def _ids_entries(value: object) -> list[dict]:
    """`{"id": "H1", "answer": …}`: the ask a seat settled, and what it settled it with."""
    raw = value if isinstance(value, list) else [value]
    out = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        token = _ids(pick(entry, "id", ("question_id", "qid", "h")))
        answer = text_of(pick(entry, "answer")) or text_of(pick(entry, "text"))
        if not token and answer:
            # only an id names an ask
            token = [one for one in _ids(pick(entry, "question")) if _ASK_ID.fullmatch(one)]
        if token:
            out.append({"id": token[0], "text": answer})
    return out


def _human_input(data: dict, refused: list[dict[str, str]]) -> list[dict]:
    raw = pick(data, "human_input")
    if isinstance(raw, str):
        raw = [{"question": raw}]
    if not isinstance(raw, list):
        raw = []
    out = []
    for entry in raw:
        if isinstance(entry, str):
            if not entry.strip():
                continue
            entry = {"question": entry}
        if not isinstance(entry, dict):
            continue
        question = text_of(pick(entry, "question"))
        if not question:
            if any(entry.values()):
                record = {"field": "human_input", "why": "carries no question text", "said": text_of(entry), "counts": "dropped"}
                if about := text_of(pick(entry, "item")).strip():
                    record["about"] = about
                refused.append(record)
            continue
        blocking = pick(entry, "blocking")
        out.append(
            {
                "question": question,
                "item": text_of(pick(entry, "item")).strip() or None,
                "blocking": _flag(blocking, "blocking", refused, about=question),
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
    return {"name": text_of(pick(value, "name")), "text": text_of(pick(value, "claim", extra=("text",))), "stages": clean}


def parse_phase0(payload: str, *, mode: str) -> Extraction:
    data, error = find_json(payload, PHASE0_FIELDS)
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
    out.data["summary"] = text_of(pick(data, "summary"))
    out.data["open_questions"] = _texts(pick(data, "open_questions"))
    out.data["notes"] = _notes(pick(data, "notes"))
    out.data["retract_note"] = []
    out.data["human_input"] = _human_input(data, out.refused)
    return out


def parse_merge_plan(payload: str) -> tuple[dict | None, list[str]]:
    data, error = find_json(payload, MERGE_FIELDS)
    if data is None:
        return None, [error or "no JSON object found"]
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
    return {"groups": clean}, []


def parse_ask_merge(payload: str) -> tuple[dict | None, list[str]]:
    """The chair's ask fold: the item merge's shape, with the docket's `H1` ids read however the chair typed them."""
    plan, errors = parse_merge_plan(payload)
    if plan is None:
        return None, errors
    groups = []
    for group in plan["groups"]:
        canonical = _ask_token(group["canonical"])
        duplicates = list(dict.fromkeys(one for one in map(_ask_token, group["duplicates"]) if one and one != canonical))
        if canonical and duplicates:
            groups.append({"canonical": canonical, "duplicates": duplicates})
    return {"groups": groups}, errors


def _ask_token(text: str) -> str:
    """`h1`, `#H1` and ` H1 ` all name H1; anything that is not an ask id names nothing."""
    token = text.strip().lstrip("#").upper()
    return token if _ASK_ID.fullmatch(token) else ""


def parse_direction(payload: str) -> tuple[dict | None, list[str]]:
    data, error = find_json(payload, DIRECTION_FIELDS)
    if data is None:
        return None, [error or "no JSON object found"]
    question = text_of(pick(data, "question")).strip()
    if not question:
        return None, ["the direction reply carried no `question`"]
    return {"question": question[:600], "context": text_of(pick(data, "context")).strip()[:600]}, []


def parse_comparison(payload: str) -> tuple[dict | None, list[str]]:
    data, error = find_json(payload, COMPARISON_FIELDS)
    if data is None:
        return None, [error or "no JSON object found"]
    out = {
        "agreed": _agreed(pick(data, "agreed")),
        "conflicts": _conflicts(pick(data, "conflicts")),
        "uncovered": _strings(pick(data, "uncovered")),
        "summary": text_of(pick(data, "summary")),
    }
    return out, []


def _agreed(value: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if isinstance(entry, str):
            point, agents = text_of(entry), []
        elif isinstance(entry, dict):
            point = text_of(pick(entry, "point")) or text_of(pick(entry, "claim"))
            listed = pick(entry, "agents") or pick(entry, "agent") or []
            agents = _strings(listed) if isinstance(listed, list) else _strings([listed])
        else:
            continue
        if not point:
            continue
        out.append({"point": point, "agents": agents})
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
                    rows.append({"agent": text_of(pick(row, "agent")), "claim": text_of(pick(row, "claim")) or text_of(pick(row, "note"))})
        out.append({"claim": text_of(pick(entry, "claim")) or text_of(pick(entry, "question")), "positions": rows})
    return out


def parse_watchdog(payload: str) -> tuple[list[dict], list[str]]:
    data, error = find_json(payload, WATCHDOG_FIELDS)
    if data is None:
        return [], [error or "no JSON object found"]
    decisions = pick(data, "decisions")
    if not isinstance(decisions, list):
        return [], ["the watchdog reply carried no `decisions` array"]
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
    return out, []


def parse_gate_check(payload: str) -> Extraction:
    """The chair's gate verification: the verdict, and what it ran and saw."""
    data, error = find_json(payload, GATE_CHECK_FIELDS)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
    out = Extraction(data={})
    if pick(data, "ok") is None:
        out.errors.append("the gate check carries no `ok` verdict")
    else:
        out.data["ok"] = _flag(pick(data, "ok"), "ok", out.refused)
    out.data["ran"] = text_of(pick(data, "ran"))
    out.data["evidence"] = text_of(pick(data, "evidence"))
    return out


def parse_work(payload: str) -> Extraction:
    """A builder's work, fix or prose-pass reply: the stage's state as the builder reports it."""
    data, error = find_json(payload, WORK_FIELDS)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
    out = Extraction(data={})
    out.data["summary"] = text_of(pick(data, "summary"))
    out.data["gate_command"] = text_of(pick(data, "gate_command"))
    out.data["gate_output"] = text_of(pick(data, "gate_output"))
    out.data["changed"] = _strings(pick(data, "changed"))
    out.data["user_text"] = _strings(pick(data, "user_text"))
    out.data["open_issues"] = _strings(pick(data, "open_issues"))
    out.data["human_input"] = _human_input(data, out.refused)
    out.data["notes"] = _notes(pick(data, "notes"))
    out.data["say_to"] = _say_to(pick(data, "say_to"), out.refused)
    out.data["addressed"] = _addressed(pick(data, "addressed"))
    if data and not (out.data["summary"] or out.data["changed"] or out.data["addressed"]):
        out.errors.append("the work reply carries no summary and names no changed files")
    return out


def _say_to(value: object, refused: list[dict[str, str]]) -> list[dict]:
    """`say_to` lines: `[{"to": "Agent2", "text": "…"}]`. A line that names no seat is refused, the rest land."""
    raw = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        who = text_of(pick(entry, "to")).strip()
        text = text_of(pick(entry, "text")).strip()
        if not text:
            continue
        if not _SEAT_ID.fullmatch(who):
            refused.append({"field": "say_to", "about": who, "why": "must be a seat ID (AgentN)", "counts": "dropped"})
            continue
        out.append({"to": who, "text": text[:1000]})
    return out


def _addressed(value: object) -> list[dict]:
    """`addressed` lines: `[{"id": "C1", "how": "…"}]` — a line that names no docket item is dropped."""
    raw = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item = text_of(pick(entry, "id")).strip()
        how = text_of(pick(entry, "how")).strip()
        if _ITEM_ID.fullmatch(item) and how:
            out.append({"id": item, "how": how[:2000]})
    return out


def parse_replan(payload: str) -> Extraction:
    """A builder's replan reply: a revised, smaller breakdown of the stuck stage."""
    data, error = find_json(payload, REPLAN_FIELDS)
    if data is None:
        return Extraction(invalid=True, errors=[error or "no JSON object found"])
    out = Extraction(data={})
    out.data["note"] = text_of(pick(data, "note")) or text_of(pick(data, "summary"))
    out.data["stages"] = _clean_stages(pick(data, "stages"))
    if not out.data["stages"]:
        out.errors.append("the replan carries no stages")
    return out


def parse_structure(payload: str) -> tuple[dict | None, list[str]]:
    """The run's chair structuring and classification replies: stages with gates, and `kind` per stage."""
    data, error = find_json(payload, STRUCTURE_FIELDS)
    if data is None:
        return None, [error or "no JSON object found"]
    stages = _clean_stages(pick(data, "stages"))
    for stage in stages:
        kind = fold(stage.get("kind"))
        stage["kind"] = kind if kind in ("code", "prose") else "code"  # a kind the chair mangled counts as code
    return {"stages": stages}, []


PICK_FIELDS = ("backing",)


def parse_pick(payload: str) -> tuple[dict | None, list[str]]:
    """The run's chair picking a direction: the token, back-mapped, with no invention."""
    data, error = find_json(payload, PICK_FIELDS)
    if data is None:
        return None, [error or "no JSON object found"]
    token, _ = map_backing(pick(data, "backing"), CHAIR_ID)
    if not token or token in (NO_PREFERENCE, DECLINED):
        return None, ["the chair picked no direction"]
    return {"backing": token}, []


def _clean_stages(value: object) -> list[dict]:
    clean: list[dict] = []
    if not isinstance(value, list):
        return clean
    for stage in value:
        if isinstance(stage, str):
            clean.append({"state": stage, "gate": "", "scope": "", "exceptions": [], "kind": "code"})
        elif isinstance(stage, dict):
            clean.append(
                {
                    "state": text_of(pick(stage, "state")),
                    "gate": text_of(pick(stage, "gate")),
                    "scope": text_of(pick(stage, "scope")),
                    "exceptions": _strings(pick(stage, "exceptions")),
                    "kind": text_of(pick(stage, "kind")),
                }
            )
    return [stage for stage in clean if stage["state"].strip() or stage["gate"].strip() or stage["kind"].strip()]


def extract(kind: str, text: str, **kwargs) -> Extraction:
    if kind == "phase0":
        return parse_phase0(text, **kwargs)
    if kind == "verdict":
        return parse_verdict(text, **kwargs)
    if kind == "merge":
        plan, errors = parse_merge_plan(text)
        return Extraction(data={"groups": (plan or {}).get("groups", [])}, invalid=plan is None, errors=errors)
    if kind == "asks":
        plan, errors = parse_ask_merge(text)
        return Extraction(data={"groups": (plan or {}).get("groups", [])}, invalid=plan is None, errors=errors)
    if kind == "direction":
        wording, errors = parse_direction(text)
        return Extraction(data=wording or {}, invalid=wording is None, errors=errors)
    if kind == "compare":
        comparison, errors = parse_comparison(text)
        return Extraction(data=comparison or {}, invalid=comparison is None, errors=errors)
    if kind == "watchdog":
        decisions, errors = parse_watchdog(text)
        return Extraction(data={"decisions": decisions}, invalid=not decisions, errors=errors)
    if kind == "work":
        return parse_work(text)
    if kind == "replan":
        return parse_replan(text)
    if kind == "structure":
        plan, errors = parse_structure(text)
        return Extraction(data=(plan or {"stages": []}), invalid=plan is None, errors=errors)
    if kind == "gate_check":
        return parse_gate_check(text)
    if kind == "pick":
        picked, errors = parse_pick(text)
        return Extraction(data=(picked or {"backing": ""}), invalid=picked is None, errors=errors)
    if kind == "prose":
        # an `explain` reply: prose is the shape, so nothing here is repairable
        return Extraction()
    raise KeyError(kind)
