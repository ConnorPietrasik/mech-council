# mech-council

## Style
- Docstrings and comments must be concise and actually helpful. No paraphrasing adjacent code, no rationale, no history.
- Remove any prose you make redundant.

## Tests
- Only write regression tests for bugs a future edit is extremely likely to reintroduce. 
- Tests assert behavior, not wording.

## Definition of Done
- ruff, mypy, and pytest must pass.

## Notes
- except A, B: without parentheses is valid Python 14; PEP 758
- Use the venv
