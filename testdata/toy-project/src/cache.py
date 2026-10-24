"""A two-tenant cache with one planted defect and one arguable design choice.

The defect is deliberate and small: `get` ignores the tenant, so one tenant reads
another's value.  The design choice — expired entries are dropped lazily, on read — is the
kind of thing a council argues about instead of merely spotting.

Deliberately **nothing here can hang a reviewer**.  An earlier version held the module
lock across a call out, and a real seat in the smoke test wrote a deadlock
reproduction, ran it, and never came back; the watchdog keeps a seat unless it is
clearly stuck, so the run waited.  A smoke test that spends tokens should fail for a
reason the code under review caused, so the hazard here is one you can read rather
than run.
"""
from __future__ import annotations

import threading
import time

_STORE: dict[str, tuple[float, object]] = {}
_LOCK = threading.RLock()


def set_value(key: str, value: object, tenant: str = "default", ttl: float = 30.0) -> None:
    with _LOCK:
        _STORE[key] = (time.monotonic() + ttl, value)


def get_value(key: str, tenant: str = "default") -> object | None:
    entry = _STORE.get(key)          # the planted defect: `tenant` is never part of the key
    if entry is None:
        return None
    expires, value = entry
    if expires < time.monotonic():
        _STORE.pop(key, None)        # lazy expiry: an argument about memory, not a hang
        return None
    return value


def keys() -> list[str]:
    """Everything ever stored that has not been *read* since it expired."""
    with _LOCK:
        return sorted(_STORE)
