"""The tests a member would run. Both pass; neither notices the planted defect, which is
the point: a green suite is not a review."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cache import get_value, keys, set_value


def test_round_trip():
    set_value("a", 1)
    assert get_value("a") == 1


def test_expiry_is_lazy():
    set_value("b", 2, ttl=0)
    assert get_value("b") is None
    assert "b" not in keys()
