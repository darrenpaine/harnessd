"""Load and validate the eval dataset (SPEC-0011 B-0001)."""

from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import ValidationError

from schemas import EvalCase
from tools.base import summarize


class DatasetError(Exception):
    """The dataset has invalid lines or duplicate ids; nothing may run."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = list(problems)


def load_cases(directory: Path) -> list[EvalCase]:
    """Every case in `directory/*.jsonl`, in file then line order. Raises
    DatasetError naming the file and line of every problem."""
    problems: list[str] = []
    cases: list[EvalCase] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            where = f"{path.name}:{number}"
            try:
                case = EvalCase.model_validate_json(line)
            except ValidationError as exc:
                problems.append(f"{where}: {summarize(exc)}")
                continue
            if case.id in seen:
                problems.append(
                    f"{where}: duplicate id {case.id} (also {seen[case.id]})"
                )
                continue
            seen[case.id] = where
            cases.append(case)
    if problems:
        raise DatasetError(problems)
    return cases


def select(
    cases: Iterable[EvalCase], tags: Sequence[str] = (), ids: Sequence[str] = ()
) -> list[EvalCase]:
    """Cases with any of `tags` or one of `ids`; all cases when neither is given."""
    if not tags and not ids:
        return list(cases)
    return [c for c in cases if c.id in ids or set(c.tags) & set(tags)]
