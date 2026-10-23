"""The prompts sent to the model, one file each (12-factor rule 2).

Each `<name>.md` in this folder holds one prompt, read once at import so a
missing file fails at startup. A change to a prompt updates the tests or evals
that cover it.
"""

from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str) -> str:
    """The prompt in `<name>.md`, without its trailing newline."""
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = load("system")
SUMMARY_PROMPT = load("summary")
