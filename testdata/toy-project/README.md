# toy-project

A two-tenant cache used as the real-pi smoke target (`tests/test_smoke.py`, in the council
suite — not in this project).  It is deliberately small: a reviewer should read all of it
in one sitting.  It holds one defect that is actually there (`tenant` is never part of the
key) and one design choice worth arguing about (expiry happens lazily, on read).

Run the tests here with `python -m pytest tests -q`, from this directory.
