# LLM reliability rules
Apply these when writing code that calls an LLM: agents, tools, prompts, and the
tests and evals for them. Based on
[12-Factor Agents](https://github.com/humanlayer/12-factor-agents) (HumanLayer).

- The project's specs and instructions win. If a change would break a rule here,
  say so and propose a spec change; don't work around either one silently.
- A rule marked "n/a" for a project is skipped, not built speculatively. Say which
  rules you skipped and why.
- Treat the model as an unreliable component behind a contract: everything it
  returns is validated, everything it may do is bounded by code.

## Rules
"Checked by" says how to verify the rule: **test** (deterministic, scripted or fake
model, runs in CI), **eval** (real model, scored over many runs), **review** (code
review only).

### Structured interactions
| # | Rule (do / don't) | Example | Checked by |
|---|---|---|---|
| 1 | Turn natural language into structured tool calls or typed output. Don't parse free text for decisions. | The model returns `{"tool": "search", "args": {...}}`, not "I will now search for…". | test: invalid output is rejected |
| 2 | Keep each prompt in its own file in `src/prompts/`, reviewed like code, including prompts for evals and judges. Code loads them through the `prompts` module; no prompt text inline in other modules. A prompt change updates the tests or evals that cover it. Don't hide prompts inside a framework's defaults. | `src/prompts/system.md`, loaded as `prompts.SYSTEM_PROMPT`; a diff to it shows in review. | review; eval on change |
| 3 | Build the context explicitly in one function: system prompt, chosen history, retrieved data, errors. Bound its size (trim, summarise, or retrieve on demand). Never send secrets or unneeded data. | `build_prompt(state) -> messages`, with a test of what it includes and leaves out. | test |
| 4 | Every tool has a typed input and output schema, validated at the boundary. Invalid model arguments become a structured error, not an exception or a guess. | Pydantic/JSON Schema models for each tool's args and result. | test |

### State and control
| # | Rule (do / don't) | Example | Checked by |
|---|---|---|---|
| 5 | Keep one source of truth for a run's state, derived from its message/event history where possible. Note any business state stored elsewhere and how it stays consistent. | The checkpoint holds messages and counters; nothing else is needed to resume. | test: resume from saved state |
| 6 | Runs can be started, paused, resumed and cancelled through a small API, with state saved between steps. | `run(thread_id, message)`, `resume(thread_id, decision)`. | test |
| 7 | Human approval is a first-class step: the run pauses with a typed request, and resumes with a typed answer. Actions with side effects or high stakes need approval unless the spec says otherwise. | Pause before "send email"; a rejection goes back to the model as a result. | test |
| 8 | Routing, limits, retries, timeouts and safety checks live in deterministic code, not in the prompt. Don't rely on "please stop after N steps". | Cap tool calls and run time in code; the model only chooses among allowed next steps. | test |

### Composition and reliability
| # | Rule (do / don't) | Example | Checked by |
|---|---|---|---|
| 9 | Feed errors back to the model as short structured messages so it can recover, and stop after a fixed number of consecutive errors. Don't send stack traces or loop forever. | `{"error": "invalid_input", "detail": "url: not a URL"}`; end the run after 2 in a row. | test |
| 10 | Keep each agent's job narrow: few tools, a focused prompt, a short loop. Split work before adding many tools to one agent. | Prefer a pipeline of small steps over one agent with 20 tools. | review; eval |
| 11 | Agent logic doesn't depend on how a run was triggered. HTTP, queues, schedules and UIs all call the same launch/resume API. | n/a if the project has one entry point. | review |
| 12 | Each step is a reducer: (state, event) → new state. Side effects happen only in tools, and their results are recorded in state. | A node returns state updates; it doesn't write files or call services directly. | test |

## Testing LLM behaviour
- Unit tests never call a real model. Use a scripted or fake model so results are
  deterministic and CI needs no keys.
- Test the code around the model: schema validation, routing, limits, error
  handling, pause/resume, context building.
- Behaviour that depends on the model (picks the right tool, answers correctly,
  refuses unsafe requests) goes in evals: fixed datasets, scored over several runs,
  with a pass threshold, kept out of the default test run.
- Log or trace every model call and tool call with its inputs' size, outputs'
  validity, tokens and latency, so failures can be found and replayed.
