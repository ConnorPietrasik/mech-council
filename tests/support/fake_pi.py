#!/usr/bin/env python3
"""§13.9/§13.12's offline member: a real child, real JSONL pipes, and no tokens spent.

The fake is deliberately dumb — canned JSON keyed on the prompt's first line — because
every §13.1/§13.12 assertion is about the orchestrator's behaviour around a member, not
about what a member says.  It is a `pi --mode rpc` replacement in exactly four commands,
and it is reachable through the real launcher, so the namespaces, mounts, journal and
sweep are all real in an "offline" test.

`FAKE_PI_SCRIPT` holds a JSON list of behaviours, applied in prompt order per member:

  {"contains": "Phase 0", "reply": {...}, "stop_reason": "stop", "tools": [...],
   "exit_after": 2, "sleep": 30, "stderr": "..."}
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

#: §4: an abort must interrupt whatever the fake is doing, the way it does a real pi.
STATE = {"abort": False}

DEFAULT_PHASE0 = {"findings": [{"claim": "the fake sees a bug", "severity": "major",
                                "evidence": "fake_pi.py:1"}],
                  "summary": "one finding", "open_questions": []}
DEFAULT_ROUND = {"positions": [], "new_items": [], "answers": [], "questions": [],
                 "human_input": [], "signoff": True}


def say(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def scripted(prompt: str, index: int):
    """The first script entry whose `contains` matches, or the index-default reply."""
    default = {"reply": dict(DEFAULT_PHASE0 if "Phase 0" in prompt else DEFAULT_ROUND)}
    raw = os.environ.get("FAKE_PI_SCRIPT")
    if not raw:
        return default
    try:
        script = json.loads(raw)
    except json.JSONDecodeError:
        return default
    entries = script if isinstance(script, list) else script.get("behaviours", [])
    for entry in entries:
        if index >= int(entry.get("index", 0)) and (
                entry.get("contains") or "") in prompt:
            return entry
    return default


def reply_to(prompt: str, index: int) -> None:
    entry = scripted(prompt, index)
    STATE["abort"] = False
    if entry.get("sleep"):
        # Poll the abort flag: a real pi stops mid-turn when the orchestrator aborts.
        deadline = time.monotonic() + float(entry["sleep"])
        while time.monotonic() < deadline and not STATE["abort"]:
            time.sleep(0.02)
        if STATE["abort"]:
            say({"type": "agent_settled", "aborted": True})
            return
    if entry.get("stderr"):
        sys.stderr.write(str(entry["stderr"]))
        sys.stderr.flush()
    if entry.get("retry"):
        say({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
             "errorMessage": entry.get("retry")})
    for tool in entry.get("tools") or []:
        say({"type": "tool_execution_start", "toolName": tool.get("name", "read"),
             "args": tool.get("args") or {}})
        say({"type": "tool_execution_end", "toolName": tool.get("name", "read"),
             "result": tool.get("result", ""), "isError": bool(tool.get("is_error"))})
    if entry.get("die"):
        os._exit(int(entry.get("die") or 1))
    body = entry.get("reply")
    if body is None:
        body = entry.get("text", "")
    text = body if isinstance(body, str) else json.dumps(body)
    if entry.get("wrap"):
        text = f"prose first\n\n```json\n{text}\n```\n"
    if entry.get("sidechain"):
        # §13.7: a sub-agent's turn ends mid-ask; only `agent_settled` ends the answer.
        say({"type": "agent_end", "isSidechain": True,
             "message": {"role": "assistant", "stopReason": "stop",
                         "content": [{"type": "text", "text": "the sub-agent's turn"}]}})
    say({"type": "message_start", "message": {"role": "assistant"}})
    say({"type": "message_end", "message": {
        "role": "assistant", "stopReason": entry.get("stop_reason", "stop"),
        **({"errorMessage": entry["error_message"]} if entry.get("error_message") else {}),
        "content": [{"type": "text", "text": text}]}})
    if not entry.get("no_settle"):
        say({"type": "agent_settled", "aborted": bool(entry.get("aborted"))})


#: written from the reader thread while the main thread may be answering a prompt.
_LOCK = threading.Lock()


def _read_stdin():
    """Yield prompt-shaped commands; answer `abort` the moment it arrives.

    A real pi takes an abort mid-turn.  If the fake only read stdin between replies, an
    abort would sit unread in the pipe for the whole turn and §4's abort path could not be
    tested at all — so the reader thread handles abort and clear_queue itself.
    """
    queue: list[str] = []
    done = threading.Event()

    def pump() -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("type") in ("abort", "clear_queue"):
                if os.environ.get("FAKE_PI_SILENT_ABORT"):
                    continue
                STATE["abort"] = True
                with _LOCK:
                    say({"type": "response", "id": command.get("id"), "success": True,
                         "data": {}})
                continue
            queue.append(line)
        done.set()

    threading.Thread(target=pump, daemon=True).start()
    while True:
        while queue:
            yield queue.pop(0)
        if done.is_set() and not queue:
            return
        time.sleep(0.01)


def main() -> int:
    """Four commands and nothing else (§4), answered one JSON object per line."""
    if "--version" in sys.argv:               # §2.4: config resolution probes this
        print("fake-pi 0.0")
        return 0
    prompts = 0
    label = os.environ.get("FAKE_PI_LABEL", os.path.basename(sys.argv[0]))
    reject = os.environ.get("FAKE_PI_REJECT_COMMANDS")
    if os.environ.get("FAKE_PI_UNKNOWN_EVENT"):
        say({"type": "the_dev_invented_this", "note": "§4: an unknown event is data"})
    if os.environ.get("FAKE_PI_STARTLE"):
        sys.stderr.write(os.environ["FAKE_PI_STARTLE"])
        sys.stderr.flush()
    for line in _read_stdin():
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, request_id = command.get("type"), command.get("id")
        if reject:
            say({"type": "response", "id": request_id, "success": False,
                 "error": f"unknown command {kind!r}"})
            continue
        if kind == "get_state":
            say({"type": "response", "id": request_id, "success": True,
                 "data": {"model": "fake/model", "thinkingLevel": "off",
                          "sessionId": f"fake-{label}", "tools": ["read"]}})
        elif kind in ("abort", "clear_queue"):
            pass                     # the reader thread answered this one already
        elif kind == "prompt":
            index = prompts
            prompts += 1
            log = os.environ.get("FAKE_PI_ARGV")
            if log and index == 0:
                # §13.5/§13.6: the suite asserts the exec'd argv and counts prompts.
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"argv": sys.argv[1:], "label": label}) + "\n")
            say({"type": "response", "id": request_id, "success": True,
                 "data": {"disposition": "accepted"}})
            reply_to(str(command.get("message") or ""), index)
            exit_after = os.environ.get("FAKE_PI_EXIT_AFTER")
            if exit_after and prompts >= int(exit_after):
                return 0
        else:
            say({"type": "response", "id": request_id, "success": False,
                 "error": f"unknown command {kind!r}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
