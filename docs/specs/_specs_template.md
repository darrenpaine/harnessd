# SPEC-NNNN: <title>

Status: draft | reviewed | implemented
Requirements: <R-ids from docs/requirements/>
Design: <link to docs/design/ file, if any>

## Goal
One or two sentences: what this does and why it exists.

## Inputs / Outputs
- Input: <request model, fields, constraints>
- Output: <response model, fields, error shape>
- Tools (if any): name, input model, output model, side effects

## Behavior
Numbered, testable statements ("The system MUST..."). B and AT ids are numbered per
spec, starting at 0001; never reuse a retired id (see SPEC-0006).
- B-0001: <statement>

## Acceptance tests
Each maps to a behavior statement and a pytest test.
- AT-0001 (B-0001): Given <x>, when <y>, then <z>. -> `tests/test_<file>.py::test_<name>`

## Non-goals
What this explicitly will not do.

## Open questions
Anything unresolved before coding.
