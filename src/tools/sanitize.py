"""Remove characters a reader can't see (SPEC-0012 B-0015).

Invisible characters can carry instructions a person reviewing the text would
never notice: zero-width spaces, bidi controls, Unicode tag characters (which
can spell out hidden ASCII) and variation selectors. They are removed from tool
output and user messages before the model sees them; visible text is unchanged.
"""

import unicodedata

from pydantic import BaseModel

# Invisible characters outside Unicode's format category (Cf), which covers
# zero-width spaces and joiners, bidi controls, the word joiner, the byte-order
# mark, the soft hyphen and the tag block.
_INVISIBLE = frozenset(
    [
        *range(0xFE00, 0xFE10),  # variation selectors
        *range(0xE0100, 0xE01F0),  # variation selectors supplement
        *range(0x180B, 0x1810),  # Mongolian free variation selectors
        0x17B4,  # Khmer inherent vowels, which render as nothing
        0x17B5,
        0x115F,  # Hangul fillers
        0x1160,
        0x3164,
        0xFFA0,
    ]
)


def is_invisible(char: str) -> bool:
    return ord(char) in _INVISIBLE or unicodedata.category(char) == "Cf"


def strip_invisible(text: str) -> tuple[str, int]:
    """`text` without invisible characters, and how many were removed."""
    if text.isascii():
        return text, 0
    kept = "".join(char for char in text if not is_invisible(char))
    return kept, len(text) - len(kept)


def strip_model[M: BaseModel](model: M) -> tuple[M, int]:
    """`model` with every string field stripped, and the total removed. The JSON
    holds non-ASCII characters as they are, and none of them is JSON syntax."""
    kept, removed = strip_invisible(model.model_dump_json())
    if not removed:
        return model, 0
    return type(model).model_validate_json(kept), removed
