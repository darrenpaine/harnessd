from pathlib import Path
from trace import ROOT, main, render, trace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GAP_KINDS = {
    ("no_acceptance_test", "B-0002"),
    ("no_test_path", "AT-0002"),
    ("missing_file", "AT-0003"),
    ("missing_test", "AT-0004"),
    ("unmarked_behavior", "B-0006"),
}


def gap_ids(spec_id: str, kind: str, message: str) -> tuple[str, str]:
    """(kind, B or AT id) from a gap message such as 'SPEC-0001 B-0002 has...'."""
    item = message.removeprefix(f"{spec_id} ").split()[0].rstrip(":")
    return kind, item


@pytest.mark.spec("SPEC-0006", "B-0001", "AT-0001")
def test_matrix_rows() -> None:
    report = trace(FIXTURES / "basic")

    assert report.errors == []
    assert report.pending == []
    row = (
        "| SPEC-0001 | reviewed | B-0001 | AT-0001 "
        "| `tests/marked_basic.py::test_one` | `tests/marked_basic.py::test_one` |"
    )
    assert row in render(report).splitlines()


@pytest.mark.spec("SPEC-0006", "B-0002", "AT-0002")
def test_gaps_by_status() -> None:
    report = trace(FIXTURES / "gaps")

    errors = {gap_ids("SPEC-0001", p.kind, p.message) for p in report.errors}
    pending = {gap_ids("SPEC-0002", p.kind, p.message) for p in report.pending}
    assert errors == GAP_KINDS
    assert pending == GAP_KINDS
    assert all("0001-gaps.md" in p.where for p in report.errors)
    assert all("0002-gaps.md" in p.where for p in report.pending)


@pytest.mark.spec("SPEC-0006", "B-0002")
def test_retired_behavior_is_not_a_gap() -> None:
    report = trace(FIXTURES / "gaps")

    assert not any("B-0007" in p.message for p in report.errors + report.pending)
    assert "| B-0007 (retired) |" in render(report)


@pytest.mark.spec("SPEC-0006", "B-0003", "AT-0003")
def test_unknown_marker_ids(tmp_path: Path) -> None:
    root = FIXTURES / "unknown"
    report = trace(root)

    messages = {problem.message for problem in report.errors}
    assert messages == {"unknown spec SPEC-0099", "unknown id SPEC-0001 B-0099"}
    assert main(["--root", str(root), "--output", str(tmp_path / "m.md")]) == 1


@pytest.mark.spec("SPEC-0006", "B-0004", "AT-0004")
def test_malformed_ids_reported() -> None:
    report = trace(FIXTURES / "malformed")

    spec = "docs/specs/0001-malformed.md"
    assert {(p.where, p.message) for p in report.errors} == {
        (f"{spec}:7", "duplicate B-0001"),
        (
            f"{spec}:10",
            "malformed id line: - AT-01 (B-1): Badly numbered. "
            "-> `tests/marked_malformed.py::test_one`",
        ),
        (f"{spec}:12", "duplicate AT-0001"),
        (f"{spec}:13", "AT-0002 names unknown B-0009"),
        ("docs/specs/0002-no-status.md", "no Status line"),
        ("tests/marked_malformed.py:14", "spec marker args must be strings"),
    }
    assert [spec.id for spec in report.specs] == ["SPEC-0001"]


@pytest.mark.spec("SPEC-0006", "B-0001")
def test_module_and_class_markers() -> None:
    report = trace(FIXTURES / "malformed")

    assert report.marked[("SPEC-0001", "B-0001")] == ["tests/marked_malformed.py"]
    assert report.marked[("SPEC-0001", "AT-0001")] == [
        "tests/marked_malformed.py::test_one"
    ]
    assert not any("Group::test_two" in p.message for p in report.pending)


@pytest.mark.spec("SPEC-0006", "B-0005", "AT-0005")
def test_ci_runs_trace() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()

    pytest_step = workflow.index("uv run pytest")
    trace_step = workflow.index("uv run python scripts/trace.py")
    assert pytest_step < trace_step


@pytest.mark.spec("SPEC-0006", "B-0001")
def test_main_writes_matrix(tmp_path: Path) -> None:
    output = tmp_path / "matrix.md"

    assert main(["--root", str(FIXTURES / "basic"), "--output", str(output)]) == 0
    assert output.read_text() == render(trace(FIXTURES / "basic"))


@pytest.mark.spec("SPEC-0005", "B-0005", "AT-0005")
def test_repo_has_no_marker_errors() -> None:
    report = trace(ROOT)

    assert [str(problem) for problem in report.errors] == []
