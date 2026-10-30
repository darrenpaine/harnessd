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

## Flagged ambiguities

- "Turn" means a user message and what follows it (SPEC-0009), while SPEC-0001 says "model turn" for one model reply. Resolved: **Turn** is the SPEC-0009 meaning; always say **Model turn** for one model reply.
- "Truncated" is a run status (the tool-call cap was reached) and must not be used for compaction. Resolved: **Compaction** is the only word for shortening history.
- "Idle" once also covered a thread with an unfinished run. Resolved: **Thread status** has three values, and SPEC-0009 reports unfinished separately.
