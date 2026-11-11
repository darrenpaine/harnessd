"""Secret values never leave the API (SPEC-0012 B-0003).

Every secret setting (a `SecretStr`) that is long enough is replaced with
`[redacted]` in answers and in the thread view. The checkpoint keeps the raw
text, and tool call records are not changed. Each response or view with a
replacement logs a `guard` audit event with the count, never the value.
"""

import re

from pydantic import SecretStr

from logs import audit
from schemas import AgentResponse, ThreadState
from settings import Settings

REDACTED = "[redacted]"
# Shorter values are left alone, so a placeholder such as `k` can't scramble
# answers.
MIN_LENGTH = 8


def secret_values(settings: Settings) -> list[str]:
    """The settings' secret values of at least MIN_LENGTH characters."""
    values = {
        value.get_secret_value()
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    }
    return [value for value in values if len(value) >= MIN_LENGTH]


class Redactor:
    def __init__(self, settings: Settings) -> None:
        # Longest first, so a secret that contains another is replaced whole.
        secrets = sorted(secret_values(settings), key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(map(re.escape, secrets))) if secrets else None
        )

    def redact(self, text: str) -> tuple[str, int]:
        """`text` with each secret value replaced, and how many were replaced."""
        if self._pattern is None:
            return text, 0
        return self._pattern.subn(REDACTED, text)

    def response(self, response: AgentResponse) -> AgentResponse:
        """The response with its answer redacted (B-0003); a truncated run's
        answer included."""
        if response.answer is None:
            return response
        answer, count = self.redact(response.answer)
        if not count:
            return response
        _log(count)
        return response.model_copy(update={"answer": answer})

    def summary(self, summary: str | None) -> str | None:
        """A compaction's summary, redacted (B-0003)."""
        if summary is None:
            return None
        text, count = self.redact(summary)
        if count:
            _log(count)
        return text

    def thread(self, state: ThreadState) -> ThreadState:
        """The thread view with its message text and summary redacted (B-0003)."""
        count = 0
        messages = []
        for message in state.messages:
            content, found = self.redact(message.content)
            count += found
            messages.append(
                message.model_copy(update={"content": content}) if found else message
            )
        summary = state.summary
        if summary is not None:
            summary, found = self.redact(summary)
            count += found
        if not count:
            return state
        _log(count)
        return state.model_copy(update={"messages": messages, "summary": summary})


def _log(count: int) -> None:
    audit("guard", check="secrets", action="redacted", count=count)
