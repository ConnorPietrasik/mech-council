"""`max_concurrency = "auto"`: ask the provider what it takes at once, and use the number it reports."""

from __future__ import annotations

import json
import os
import urllib.parse

PROBE_TIMEOUT_S = 5.0

#: (path, dotted field) pairs a provider might report its limit under, first answer wins.
LIMIT_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (("server_info", ("server", "max_num_seqs")),)


def models_config_path() -> str:
    """The models.json pi reads: the agent dir's — pi has no project-level models.json."""
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(os.path.expanduser("~"), ".pi", "agent")
    return os.path.join(agent_dir, "models.json")


def provider_entry(name: str) -> dict | None:
    """The provider's entry in pi's models.json, or None: the file lacks it (a built-in provider).

    Raises ValueError if the file exists but is not a provider table: it does not parse, or its top level or 'providers' is not an object.
    """
    try:
        with open(models_config_path(), "rb") as handle:
            raw = json.load(handle)
    except OSError:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"top level is {type(raw).__name__}, not an object")
    providers = raw.get("providers")
    if providers is not None and not isinstance(providers, dict):
        raise ValueError(f"'providers' is {type(providers).__name__}, not an object")
    entry = (providers or {}).get(name)
    return entry if isinstance(entry, dict) else None


def probe(name: str) -> tuple[int | None, str | None]:
    """The provider's reported concurrency, or (None, why) when it reports none. Never raises: auto falls back."""
    try:
        entry = provider_entry(name)
    except json.JSONDecodeError as failure:
        return None, f"models.json at {models_config_path()} does not parse ({failure}), so its entry cannot be read"
    except ValueError as failure:
        return None, f"models.json at {models_config_path()} is malformed ({failure}), so its entry cannot be read"
    if entry is None:
        return None, "no models.json entry states its baseUrl, so there is no endpoint to ask"
    base = entry.get("baseUrl")
    if not isinstance(base, str):
        return None, "its models.json entry states no baseUrl, so there is no endpoint to ask"
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, f"its baseUrl {base!r} is not an http(s) endpoint"
    origin = f"{parts.scheme}://{parts.netloc}"
    auth = _auth(entry.get("apiKey"))
    answered = False
    for path, dotted in LIMIT_PROBES:
        payload = _fetch(f"{origin}/{path}", auth)
        if payload is None:
            continue
        answered = True
        if limit := _dig(payload, dotted):
            return limit, None
    if answered:
        return None, f"it answers at {origin} but reports no recognized limit"
    return None, f"it answers no limit report at {origin}"


def _auth(api_key: object) -> str | None:
    """A Bearer header from the entry's apiKey: a literal, or `$NAME`/`${NAME}` read from the environment.
    `!command` is pi's to run, and "none" a placeholder: the probe goes unauthenticated then."""
    if not isinstance(api_key, str) or api_key in ("", "none") or api_key.startswith("!"):
        return None
    if api_key.startswith("${") and api_key.endswith("}"):
        key = os.environ.get(api_key[2:-1], "")
    elif api_key.startswith("$"):
        key = os.environ.get(api_key[1:], "")
    else:
        key = api_key
    return f"Bearer {key}" if key else None


def _fetch(url: str, auth: str | None) -> object | None:
    # imported here: `urllib.request` pulls platform modules that a simulated foreign platform lacks
    import urllib.request

    headers = {"Authorization": auth} if auth else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=PROBE_TIMEOUT_S) as response:
            return json.load(response)
    except OSError, ValueError:
        return None


def _dig(payload: object, dotted: tuple[str, ...]) -> int | None:
    node: object = payload
    for key in dotted:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, int) and not isinstance(node, bool) and node >= 1:
        return node
    return None
