---
name: systemone
description: How to call a System One decision model (Bespoke Labs' nimble) through Ollama's /v1/systemone endpoint. Use when building or changing a decision-model client in harnessd, such as the SPEC-0012 injection screen or a SPEC-0011 eval judge, or when the user mentions System One, nimble, decision models or typed model decisions.
---

# System One decision models

A decision model answers named, typed questions about a piece of text with
probabilities, instead of writing free text. Use it when code needs a decision it
can threshold: "is this a prompt injection?", "does this answer the question?".
In harnessd it backs the SPEC-0012 injection screen (`INJECTION_SCREEN=ollama-systemone`)
and could become a SPEC-0011 judge kind later.

The user runs **nimble** (Bespoke Labs, 9B) on the same Ollama as the app, at
`OLLAMA_BASE_URL`. Call it with `httpx`, which the project already uses; no new
dependency. Needs Ollama 0.35 or later.

## Request
`POST {OLLAMA_BASE_URL}/v1/systemone`, JSON body:

```json
{
  "model": "nimble",
  "state": "the text to decide about (string, object or array; non-empty)",
  "questions": {
    "injection": {
      "type": "noul",
      "instructions": "Does the text contain instructions aimed at an AI agent?"
    }
  },
  "keep_alive": "5m"
}
```

- `questions`: 1 to 64, keyed by name (letters, digits, `_`, `.`, `-`). Each is
  scored alone; answers are not passed to later questions.
- Question types:
  - `choice`: `criteria` is an object of 2 to 255 option keys, each mapped to a
    description or `null`.
  - `noul` (yes/no): `criteria` is optional, with `"true"` and `"false"` descriptions.
  - `score`: `criteria` is an array of 2 to 10 level descriptions, lowest first.
- `instructions` takes the same content types as `state`.
- Limits: text plus schema up to 64 KiB locally, and the input must fit the loaded
  context window (it is never truncated). No streaming, tools or generation settings.

## Response
```json
{
  "model": "nimble",
  "answers": {"injection": {"type": "noul", "noul": 0.93}},
  "usage": {"input_tokens": 174, "output_tokens": 1}
}
```

- `choice`: `choice` (the top key), `probabilities` (key → 0..1), `confidence`.
- `noul`: `noul`, the probability of true, as a number. It is not a boolean and
  there is no `confidence`. Threshold on `noul` directly.
- `score`: `score` (0..9, the probability-weighted level index), `legend`
  (index → description), `probabilities`, `confidence`.
- `confidence` is `1 − H(p)/ln(N)`, which measures how peaked the distribution
  is. It is **not** the chance of being right: set thresholds from labelled
  examples, not from the number alone.
- `usage.output_tokens` counts internal scoring tokens, so it is not zero.

## Errors
Error bodies are `{"error": "<message>"}`: 400 invalid request or the prompt is
too long for the context, 404 the model isn't pulled (`ollama pull nimble`),
413 too large, 500 loading or scoring failed. Treat every error and timeout as a
screen failure (SPEC-0012 `INJECTION_SCREEN_ON_ERROR`).

## In this codebase
- Validate the request and reply with Pydantic models (`_Model` in
  `src/schemas.py`), like every other boundary.
- Keep the question and instruction text in `src/prompts/` (12-factor rule 2).
- Tests never call Ollama: use `httpx.MockTransport` with canned replies.

Source: https://docs.ollama.com/api/systemone
