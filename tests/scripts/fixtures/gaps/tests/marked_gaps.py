import pytest


@pytest.mark.spec("SPEC-0001", "B-0001", "B-0002", "B-0003", "B-0004", "B-0005")
@pytest.mark.spec("SPEC-0002", "B-0001", "B-0002", "B-0003", "B-0004", "B-0005")
def test_one() -> None:
    pass


def test_unmarked() -> None:
    pass
