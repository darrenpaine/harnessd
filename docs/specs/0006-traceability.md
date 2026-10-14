# SPEC-0006: Traceability

Status: reviewed
Requirements: none yet
Design: none yet

## Goal
Every requirement can be followed to the spec behaviors that meet it and the tests
that prove them, and gaps are found by a script rather than by reading.

## Inputs / Outputs
### ID scheme
| Id | Where | Scope | Example |
|----|-------|-------|---------|
| R-nnnn | `docs/requirements/*.md` | global | R-0003 |
| SPEC-nnnn | `docs/specs/nnnn-<name>.md` | global | SPEC-0002 |
| B-nnnn | a spec's Behavior section | per spec | SPEC-0002 B-0003 |
| AT-nnnn | a spec's Acceptance tests section | per spec | SPEC-0002 AT-0004 |

- B and AT numbers restart in each spec, so outside their own spec they are always
  written with the spec id ("SPEC-0002 B-0003").
- IDs are never reused. A removed behavior or test stays in the spec marked
  `(retired)`.
- A spec lists the requirements it meets in its `Requirements:` header.

### Formats the script relies on
- Behavior: `- B-nnnn: <statement>`
- Acceptance test: `- AT-nnnn (B-nnnn[, B-nnnn]): <description> -> \`<test path>\``
  where the test path is `tests/<file>.py` or `tests/<file>.py::<test_name>`.
- Test marker: `@pytest.mark.spec("SPEC-nnnn", "B-nnnn", "AT-nnnn", ...)` — one marker
  per spec, stacked when a test covers more than one spec.

### Script
`scripts/trace.py` (stdlib only), run with `uv run python scripts/trace.py`.
- Reads every spec in `docs/specs/` (skipping `_` templates and README).
- Reads test markers by parsing `tests/**/*.py` with `ast` (no test imports).
- Writes `docs/traceability.md`: one row per behavior with its spec, status, ATs, AT
  test paths, and the tests whose markers name it.
- Exit code 0 when there are no errors, 1 otherwise.

## Behavior
- B-0001: The script lists each spec's behaviors and acceptance tests, and the tests
  whose markers reference them, in `docs/traceability.md`.
- B-0002: Gaps are: a behavior with no AT; an AT with no test path; an AT test path
  whose file or test function doesn't exist; a behavior no test marker names. For specs
  with `Status: implemented` gaps are errors; for `draft` and `reviewed` specs they are
  listed as pending.
- B-0003: A marker that names an unknown spec, behavior or AT is always an error,
  whatever the spec's status.
- B-0004: A malformed ID or AT line in a spec is reported with file and line number.

## Acceptance tests
All use small fixture spec and test folders under `tests/scripts/fixtures/`.
- AT-0001 (B-0001): Fixture with one spec and one marked test → matrix row lists the
  behavior, its AT and the test. -> `tests/scripts/test_trace.py::test_matrix_rows`
- AT-0002 (B-0002): Each gap type is an error for an implemented spec and pending for
  a reviewed one. -> `tests/scripts/test_trace.py::test_gaps_by_status`
- AT-0003 (B-0003): Marker naming SPEC-0099 or B-0099 → error and exit code 1.
  -> `tests/scripts/test_trace.py::test_unknown_marker_ids`
- AT-0004 (B-0004): Spec with `- AT-01 (B-1)` → error naming file and line.
  -> `tests/scripts/test_trace.py::test_malformed_ids_reported`

## Non-goals
A web UI for the matrix; linking to issues or PRs; tracing design docs.

## Open questions
- When should CI run the script? Proposed: once it's implemented, as a CI step that
  fails on errors (pending gaps don't fail).
