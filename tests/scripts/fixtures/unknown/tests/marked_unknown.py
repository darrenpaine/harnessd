import pytest


@pytest.mark.spec("SPEC-0001", "B-0001", "AT-0001")
def test_known() -> None:
    pass


@pytest.mark.spec("SPEC-0099", "B-0001")
def test_unknown_spec() -> None:
    pass


@pytest.mark.spec("SPEC-0001", "B-0099")
def test_unknown_behavior() -> None:
    pass
