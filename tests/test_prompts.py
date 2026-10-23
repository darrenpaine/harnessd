"""The prompt files in src/prompts load (12-factor rule 2)."""

from pathlib import Path

import prompts


def test_prompt_files_load() -> None:
    files = sorted(Path(prompts.__file__).parent.glob("*.md"))
    assert [f.stem for f in files] == ["summary", "system"]
    for file in files:
        text = prompts.load(file.stem)
        assert text, f"{file.name} is empty"
        assert text == text.strip(), f"{file.name}: surrounding whitespace kept"
