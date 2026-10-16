"""Build the spec traceability matrix and report gaps (SPEC-0006).

Reads the specs in docs/specs/ and the spec markers in tests/, writes
docs/traceability.md, and exits 1 when there are errors.
"""

import argparse
import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = Path("docs") / "traceability.md"

TITLE = re.compile(r"^# (SPEC-\d{4}):")
STATUS = re.compile(r"^Status: (draft|reviewed|implemented)\s*$")
ID_LINE = re.compile(r"^- (B|AT)-")
B_LINE = re.compile(r"^- (B-\d{4}): \S")
AT_LINE = re.compile(r"^- (AT-\d{4}) \((B-\d{4}(?:, B-\d{4})*)\): \S")
BACKTICKED = re.compile(r"`([^`]+)`")
RETIRED = "(retired)"


@dataclass
class Behavior:
    id: str
    line: int
    retired: bool


@dataclass
class Acceptance:
    id: str
    line: int
    behaviors: list[str]
    paths: list[str]
    retired: bool


@dataclass
class Spec:
    id: str
    file: str
    status: str
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    acceptance: dict[str, Acceptance] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    where: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass
class Marker:
    test: str
    where: str
    args: list[str]


@dataclass
class Report:
    specs: list[Spec]
    marked: dict[tuple[str, str], list[str]]
    errors: list[Problem]
    pending: list[Problem]


def read_specs(root: Path, errors: list[Problem]) -> list[Spec]:
    specs = []
    for path in sorted((root / "docs" / "specs").glob("*.md")):
        if path.name.startswith("_") or path.name == "README.md":
            continue
        spec = parse_spec(path, path.relative_to(root).as_posix(), errors)
        if spec is not None:
            specs.append(spec)
    return specs


def parse_spec(path: Path, file: str, errors: list[Problem]) -> Spec | None:
    lines = path.read_text().splitlines()
    spec_id = status = None
    for text in lines:
        if spec_id is None and (title := TITLE.match(text)):
            spec_id = title.group(1)
        if status is None and (state := STATUS.match(text)):
            status = state.group(1)
    if spec_id is None or status is None:
        missing = "title `# SPEC-nnnn:`" if spec_id is None else "Status line"
        errors.append(Problem(file, "malformed", f"no {missing}"))
        return None
    spec = Spec(spec_id, file, status)
    for index, text in enumerate(lines):
        if not ID_LINE.match(text):
            continue
        where = f"{file}:{index + 1}"
        entry = " ".join([text, *continuation(lines, index)])
        if behavior := B_LINE.match(text):
            found_b = Behavior(behavior.group(1), index + 1, RETIRED in entry)
            add_behavior(spec, found_b, where, errors)
        elif acceptance := AT_LINE.match(text):
            _, _, after = entry.rpartition("->")
            paths = BACKTICKED.findall(after) if "->" in entry else []
            found = Acceptance(
                acceptance.group(1),
                index + 1,
                acceptance.group(2).split(", "),
                paths,
                RETIRED in entry,
            )
            add_acceptance(spec, found, where, errors)
        else:
            errors.append(
                Problem(where, "malformed", f"malformed id line: {text.strip()}")
            )
    for found in spec.acceptance.values():
        for behavior_id in found.behaviors:
            if behavior_id not in spec.behaviors:
                where = f"{file}:{found.line}"
                errors.append(
                    Problem(
                        where, "malformed", f"{found.id} names unknown {behavior_id}"
                    )
                )
    return spec


def continuation(lines: list[str], index: int) -> list[str]:
    """The indented lines that continue the list item at `index`."""
    rest = []
    for text in lines[index + 1 :]:
        if not text.startswith("  "):
            break
        rest.append(text.strip())
    return rest


def add_behavior(
    spec: Spec, behavior: Behavior, where: str, errors: list[Problem]
) -> None:
    if behavior.id in spec.behaviors:
        errors.append(Problem(where, "malformed", f"duplicate {behavior.id}"))
        return
    spec.behaviors[behavior.id] = behavior


def add_acceptance(
    spec: Spec, found: Acceptance, where: str, errors: list[Problem]
) -> None:
    if found.id in spec.acceptance:
        errors.append(Problem(where, "malformed", f"duplicate {found.id}"))
        return
    spec.acceptance[found.id] = found


def read_markers(root: Path, errors: list[Problem]) -> list[Marker]:
    markers = []
    tests = root / "tests"
    for path in sorted(tests.rglob("*.py")):
        if "fixtures" in path.relative_to(tests).parts:
            continue
        file = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), filename=file)
        markers.extend(module_markers(tree, file, errors))
    return markers


def module_markers(tree: ast.Module, file: str, errors: list[Problem]) -> list[Marker]:
    markers = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            values = (
                node.value.elts if isinstance(node.value, ast.List) else [node.value]
            )
            markers.extend(spec_markers(values, file, file, errors))
    for name, decorators in decorated(tree.body, ""):
        markers.extend(spec_markers(decorators, f"{file}::{name}", file, errors))
    return markers


def decorated(body: list[ast.stmt], prefix: str) -> list[tuple[str, list[ast.expr]]]:
    """Every function and class in `body` with its decorators, classes recursed."""
    found = []
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.append((prefix + node.name, node.decorator_list))
        if isinstance(node, ast.ClassDef):
            found.extend(decorated(node.body, f"{prefix}{node.name}::"))
    return found


def spec_markers(
    nodes: list[ast.expr], test: str, file: str, errors: list[Problem]
) -> list[Marker]:
    markers = []
    for node in nodes:
        if not (
            isinstance(node, ast.Call) and ast.unparse(node.func).endswith("mark.spec")
        ):
            continue
        where = f"{file}:{node.lineno}"
        args = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        if len(args) != len(node.args) or not all(isinstance(a, str) for a in args):
            errors.append(Problem(where, "marker", "spec marker args must be strings"))
            continue
        markers.append(Marker(test, where, [str(arg) for arg in args]))
    return markers


def defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def path_problem(root: Path, path: str) -> tuple[str, str] | None:
    """Why an AT's test path doesn't resolve, as (kind, message), or None."""
    file, _, name = path.partition("::")
    target = root / file
    if not target.is_file():
        return "missing_file", f"no file {file}"
    if name and name.split("::")[-1] not in defined_names(target):
        return "missing_test", f"no test {name} in {file}"
    return None


def check_markers(
    specs: dict[str, Spec], markers: list[Marker], errors: list[Problem]
) -> dict[tuple[str, str], list[str]]:
    marked: dict[tuple[str, str], list[str]] = defaultdict(list)
    for marker in markers:
        spec_id, *ids = marker.args or [""]
        spec = specs.get(spec_id)
        if spec is None:
            errors.append(
                Problem(marker.where, "marker", f"unknown spec {spec_id or '(none)'}")
            )
            continue
        for item in ids:
            known = item in spec.behaviors or item in spec.acceptance
            if not known:
                errors.append(
                    Problem(marker.where, "marker", f"unknown id {spec_id} {item}")
                )
            elif marker.test not in marked[(spec_id, item)]:
                marked[(spec_id, item)].append(marker.test)
    return marked


def find_gaps(
    root: Path, spec: Spec, marked: dict[tuple[str, str], list[str]]
) -> list[Problem]:
    gaps = []
    covered = {b for found in spec.acceptance.values() for b in found.behaviors}
    for behavior in spec.behaviors.values():
        if behavior.retired:
            continue
        where = f"{spec.file}:{behavior.line}"
        if behavior.id not in covered:
            message = f"{spec.id} {behavior.id} has no acceptance test"
            gaps.append(Problem(where, "no_acceptance_test", message))
        if not marked.get((spec.id, behavior.id)):
            message = f"{spec.id} {behavior.id} is named by no test marker"
            gaps.append(Problem(where, "unmarked_behavior", message))
    for found in spec.acceptance.values():
        if found.retired:
            continue
        where = f"{spec.file}:{found.line}"
        if not found.paths:
            message = f"{spec.id} {found.id} has no test path"
            gaps.append(Problem(where, "no_test_path", message))
        for path in found.paths:
            if problem := path_problem(root, path):
                kind, reason = problem
                gaps.append(Problem(where, kind, f"{spec.id} {found.id}: {reason}"))
    return gaps


def trace(root: Path) -> Report:
    errors: list[Problem] = []
    pending: list[Problem] = []
    specs = read_specs(root, errors)
    by_id = {spec.id: spec for spec in specs}
    marked = check_markers(by_id, read_markers(root, errors), errors)
    for spec in specs:
        gaps = find_gaps(root, spec, marked)
        (errors if spec.status == "implemented" else pending).extend(gaps)
    return Report(specs, dict(marked), errors, pending)


def render(report: Report) -> str:
    lines = [
        "# Traceability",
        "",
        "Generated by `scripts/trace.py` (SPEC-0006); do not edit by hand.",
        "",
        "| Spec | Status | Behavior | Acceptance tests | Test paths | Marked tests |",
        "|------|--------|----------|------------------|------------|--------------|",
    ]
    for spec in report.specs:
        for behavior in spec.behaviors.values():
            tests = [
                found
                for found in spec.acceptance.values()
                if behavior.id in found.behaviors
            ]
            paths = [path for found in tests for path in found.paths]
            name = behavior.id + (" (retired)" if behavior.retired else "")
            cells = [
                spec.id,
                spec.status,
                name,
                cell([found.id for found in tests]),
                cell(paths, code=True),
                cell(report.marked.get((spec.id, behavior.id), []), code=True),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    for title, problems in (("Errors", report.errors), ("Pending", report.pending)):
        lines += ["", f"## {title}", ""]
        lines += [f"- {problem}" for problem in problems] or ["None."]
    return "\n".join(lines) + "\n"


def cell(items: list[str], code: bool = False) -> str:
    if not items:
        return "—"
    return "<br>".join(f"`{item}`" if code else item for item in items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--output", type=Path, help="matrix file to write")
    args = parser.parse_args(argv)
    root: Path = args.root
    output: Path = args.output or root / OUTPUT
    report = trace(root)
    output.write_text(render(report))
    for problem in report.errors:
        print(f"error: {problem}", file=sys.stderr)
    print(
        f"{sum(len(spec.behaviors) for spec in report.specs)} behaviors, "
        f"{len(report.errors)} errors, {len(report.pending)} pending; wrote {output}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
