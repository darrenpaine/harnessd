# harnessd

A service that runs one tool-using agent over conversations, within limits on what
each conversation may do and spend. These are the words the specs, the design and
the code use for it.

## Language

### Conversations

**Thread**:
One conversation with the agent, kept between requests so it can be continued.
_Avoid_: session, conversation, chat

**Run**:
The agent's work on one request to a thread: from a user message or a confirmation
until it answers, pauses or stops.
_Avoid_: invocation, request (the request is what starts a run), turn

**Model call**:
One prompt sent to a chat model and the reply it returns. A run makes one or more;
compaction and the injection screen can each make one too.
_Avoid_: completion, inference, LLM request

**Pending action**:
A tool call with a side effect that a run has paused on, waiting for a person to
approve or reject it.
_Avoid_: confirmation (the person's answer), approval request

**Compaction**:
Replacing a thread's older messages with a summary written by the model.
_Avoid_: summarisation, trimming, pruning

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
A chat model together with its price and the day's spend, so that every model call
made through it is counted, priced and added to the spend.
_Avoid_: model wrapper, billing client, tracked model

### Guardrails

**Injection screen**:
A check on a tool's result that gives the probability the text is trying to direct
the agent that reads it.
_Avoid_: injection filter, injection detector, classifier
