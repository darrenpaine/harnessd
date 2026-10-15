# SPEC-0003: Tool contract

Status: reviewed
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
One standard shape for every tool the agent can call, so tools are typed, bounded in
time, safe to retry where possible, and never crash the graph. Also specifies the
three tools in this service.

## Inputs / Outputs
### Contract
Each tool is one module in `src/tools/` and is registered with:
- name: str, unique, snake_case.
- Input and Output Pydantic models; unknown fields rejected.
- side_effect: bool — true means the tool changes something outside the service and
  needs human confirmation before it runs (SPEC-0001 B-0005).
- idempotent: bool — true means repeating a call with the same input is harmless.
- timeout_seconds: float > 0.

A tool returns its Output model or a ToolError; it never raises.

ToolError: code, message.
| Code | Meaning |
|------|---------|
| invalid_input | Arguments failed the Input model |
| timeout | Exceeded timeout_seconds |
| upstream_error | An external service failed or is not configured |
| rejected | A human declined the confirmation (set by the graph) |
| internal | Any other unexpected exception |

ToolCallRecord (one entry in the trace): id, name, input (the tool's Input model),
output (its Output model) | null, error (ToolError) | null — exactly one of output and
error is set.

## Behavior
- B-0001: A tool never raises into the graph. Arguments that fail the Input model
  return ToolError `invalid_input`; any unexpected exception returns ToolError `internal`.
- B-0002: A call that runs longer than the tool's timeout_seconds is stopped and
  returns ToolError `timeout`.
- B-0003: Every registered tool declares a name, Input and Output models, side_effect,
  idempotent and timeout_seconds; names are unique.
- B-0004: Read-only tools are idempotent and safe to retry. A side-effecting tool runs
  at most once per approved confirmation: resuming or retrying a checkpointed thread
  never repeats a completed side effect.
- B-0005: web_search behaves as specified below.
- B-0006: fetch_site behaves as specified below.
- B-0007: add_note behaves as specified below.

## Tools in this service
| Tool | Input | Output | side_effect | idempotent | timeout |
|------|-------|--------|-------------|------------|---------|
| web_search | query: str 1..400, max_results: int 1..5 = 3 | results: list[{title, url, snippet}] | no | yes | 10 s |
| fetch_site | url: http(s) URL | url, text: str ≤1000 chars, truncated: bool | no | yes | 10 s |
| add_note | text: str 1..500, single line | written: bool, line: str | yes | no | 2 s |

- web_search: Tavily search. A missing `TAVILY_API_KEY` returns ToolError
  `upstream_error`.
- fetch_site: returns visible text extracted from HTML (or plain text), cut to 1000
  chars with `truncated` set. At most 3 redirects. Rejects non-text content and
  private, loopback and link-local addresses (including after redirects).
- add_note: appends `<ISO-8601 UTC timestamp>\t<text>\n` to `NOTES_PATH` (default
  `data/notes.txt`), creating the file and folder if needed.

## Acceptance tests
- AT-0001 (B-0001): For every registered tool, invalid args → `invalid_input`; an
  injected exception → `internal`; nothing raises.
  -> `tests/tools/test_contract.py::test_tool_errors_are_structured`
- AT-0002 (B-0002): A tool that sleeps past its timeout → `timeout`.
  -> `tests/tools/test_contract.py::test_tool_timeout`
- AT-0003 (B-0003): Every registered tool declares the metadata; names are unique.
  -> `tests/tools/test_contract.py::test_registry_declares_metadata`
- AT-0004 (B-0004): Confirm add_note, then resume the same thread again → one line in
  the file. -> `tests/test_confirmation.py::test_side_effect_runs_once`
- AT-0005 (B-0005): Mocked Tavily response maps to WebSearchOutput; missing key →
  `upstream_error`. -> `tests/tools/test_web_search.py`
- AT-0006 (B-0006): fetch_site (HTTP mocked) extracts text, truncates to 1000 chars,
  rejects private IPs, non-text content and redirects to private IPs.
  -> `tests/tools/test_fetch_site.py`
- AT-0007 (B-0007): add_note writes one correctly formatted line to a temp NOTES_PATH.
  -> `tests/tools/test_add_note.py`

## Non-goals
Dynamic tool loading, per-user tool permissions, tool retries inside the tool.

## Open questions
None.
