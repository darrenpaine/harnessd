# SPEC-0001: Malformed

Status: reviewed

## Behavior
- B-0001: Fine.
- B-0001: Duplicate.

## Acceptance tests
- AT-01 (B-1): Badly numbered. -> `tests/marked_malformed.py::test_one`
- AT-0001 (B-0001): Fine. -> `tests/marked_malformed.py::test_one`
- AT-0001 (B-0001): Duplicate. -> `tests/marked_malformed.py::test_one`
- AT-0002 (B-0009): Names an unknown behavior.
  -> `tests/marked_malformed.py::Group::test_two`
