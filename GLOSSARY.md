# harnessd

A service that runs one tool-using agent in conversations that persist between
requests, and returns validated, structured results.

## Language

### Conversations

**Thread**:
One conversation with the agent, named by a thread id: its messages, summary and any pending action. An unknown or expired id starts a new thread.
_Avoid_: session, conversation id, chat

**Run**:
One request's work on a thread: from a user message or a confirmation decision until the agent answers, pauses, is truncated, fails or times out.
_Avoid_: invocation, execution, job

**Turn**:
A user message and everything after it up to the next user message. Compaction keeps and removes whole turns.
_Avoid_: exchange, round

**Model turn**:
One reply from the model within a run, with the tool calls it requests.
_Avoid_: step, turn (on its own)

**Model call**:
One prompt sent to a chat model and the reply it returns. A run makes one for each model turn; compaction and the injection screen can each make one too.
_Avoid_: completion, inference, LLM request

**Thread status**:
Where a thread stands between runs: idle (ready for a message), awaiting confirmation (paused on a pending action) or unfinished (a run stopped part way, e.g. on a timeout).
_Avoid_: state (on its own), run status

### Human oversight

**Side effect**:
A change outside the service that a tool call would make, such as writing a note.
_Avoid_: mutation, write

**Pending action**:
A tool call with a side effect that a run has paused on, waiting for a human to approve or reject it.
_Avoid_: interrupt, pending tool call

**Confirmation**:
A human's approve or reject decision on a pending action, which resumes the run. A rejection reaches the model as a rejected tool result.
_Avoid_: approval (on its own), resume

### History

**Compaction**:
Replacing a thread's older turns with a summary, so the history sent to the model stays bounded.
_Avoid_: trimming, pruning, truncation

**Summary**:
The model-written text standing in for a thread's compacted turns; each compaction folds in the previous summary.
_Avoid_: digest, recap

### Money and limits

**Price**:
What a model charges per million tokens, for input and for output.
_Avoid_: rate, cost

**Cost**:
The money for one model call, or the sum over the model calls of one run.
_Avoid_: price, spend

**Spend**:
The cost accumulated over one UTC day, across every thread.
_Avoid_: cost, usage

**Token budget**:
A cap in tokens: on one run, and on one thread since its last compaction.
_Avoid_: token limit, quota

**Daily cost limit**:
The cap in money on a day's spend.
_Avoid_: daily budget, spending cap

**Budget stop**:
A run ending early because a token budget or the daily cost limit was reached.
_Avoid_: limit error, quota error

**Metered model**:
A chat model together with its price and the day's spend, so that every model call made through it is counted, priced and added to the spend.
_Avoid_: model wrapper, billing client, tracked model

### Guardrails

**Injection screen**:
A check on a tool's result that gives the probability the text is trying to direct the agent that reads it.
_Avoid_: injection filter, injection detector, classifier

## Flagged ambiguities

- "Turn" means a user message and what follows it (SPEC-0009), while SPEC-0001 says "model turn" for one model reply. Resolved: **Turn** is the SPEC-0009 meaning; always say **Model turn** for one model reply.
- "Truncated" is a run status (the tool-call cap was reached) and must not be used for compaction. Resolved: **Compaction** is the only word for shortening history.
- "Idle" once also covered a thread with an unfinished run. Resolved: **Thread status** has three values, and SPEC-0009 reports unfinished separately.
- "Model call" and "model turn" overlap. Resolved: a **Model turn** is a reply within a run's loop; a **Model call** is any prompt sent to a chat model, so compaction's summary and the injection screen make model calls that are not model turns.
- "Budget" and "limit" were both used for all three caps. Resolved: a **Token budget** is in tokens (run, thread); the **Daily cost limit** is in money; a **Budget stop** is a run ending on any of them, as the `budget.stop` event and the `budget_exceeded` error code already say.
