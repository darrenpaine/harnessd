import pytest

pytestmark = [pytest.mark.spec("SPEC-0001", "B-0001")]

ID = "B-0001"


@pytest.mark.spec("SPEC-0001", "AT-0001")
def test_one() -> None:
    pass


class Group:
    @pytest.mark.spec("SPEC-0001", ID)
    def test_two(self) -> None:
        pass
