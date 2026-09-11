"""The stub convention in ``CLAUDE.md`` holds for every stub in the package.

A stub raises ``NotImplementedError`` with a message naming the fully qualified
callable and pointing at ``docs/roadmap.md`` with a phase. This test walks the
package by AST rather than importing and calling anything, so it covers stubs
that no other test reaches and stays green as modules land and the set shrinks.

Abstract methods are exempt. A bare ``raise`` under ``@abstractmethod`` means a
subclass is incomplete, not that a phase is unfinished, and the message rule
would mislead a reader about which of those two things went wrong.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import fbe

PACKAGE_ROOT = Path(fbe.__file__).resolve().parent
ROADMAP = Path(__file__).resolve().parents[1] / "docs" / "roadmap.md"

MESSAGE = re.compile(
    r"^(?P<callable>fbe(?:\.\w+)+) is scaffolded; "
    r"see docs/roadmap\.md Phase (?P<phase>\d+)$"
)


@dataclass(frozen=True)
class RaiseSite:
    """One ``raise NotImplementedError`` statement and where it sits."""

    location: str
    qualname: str
    abstract: bool
    message: str | None


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_abstract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "abstractmethod":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "abstractmethod":
            return True
    return False


def _raises_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


def _message(node: ast.Raise) -> str | None:
    if not isinstance(node.exc, ast.Call) or not node.exc.args:
        return None
    first = node.exc.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _walk(
    node: ast.AST, path: Path, scope: tuple[str, ...], abstract: bool
) -> list[RaiseSite]:
    sites: list[RaiseSite] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            sites.extend(_walk(child, path, (*scope, child.name), False))
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            sites.extend(_walk(child, path, (*scope, child.name), _is_abstract(child)))
        elif isinstance(child, ast.Raise) and _raises_not_implemented(child):
            sites.append(
                RaiseSite(
                    location=f"{path.relative_to(PACKAGE_ROOT.parent)}:{child.lineno}",
                    qualname=".".join(scope),
                    abstract=abstract,
                    message=_message(child),
                )
            )
        else:
            sites.extend(_walk(child, path, scope, abstract))
    return sites


def raise_sites() -> list[RaiseSite]:
    sites: list[RaiseSite] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites.extend(_walk(tree, path, (_module_name(path),), False))
    return sites


def roadmap_phases() -> set[str]:
    return set(re.findall(r"^## Phase (\d+):", ROADMAP.read_text(), re.MULTILINE))


def test_roadmap_declares_phases() -> None:
    """Guards the phase check below: an empty set would pass vacuously."""
    assert roadmap_phases() >= {"0", "1"}


def test_every_stub_names_itself_and_points_at_a_roadmap_phase() -> None:
    phases = roadmap_phases()
    failures: list[str] = []
    for site in raise_sites():
        if site.abstract:
            continue
        if site.message is None:
            failures.append(f"{site.location}: bare raise in {site.qualname}")
            continue
        match = MESSAGE.match(site.message)
        if match is None:
            failures.append(f"{site.location}: {site.message!r} is not the form")
            continue
        if match["callable"] != site.qualname:
            failures.append(
                f"{site.location}: message names {match['callable']}, "
                f"raised from {site.qualname}"
            )
        if match["phase"] not in phases:
            failures.append(f"{site.location}: no Phase {match['phase']} in roadmap")
    assert not failures, "\n".join(failures)


def test_abstract_methods_keep_a_bare_raise() -> None:
    """The exemption is deliberate, so a message on an abstract method is a slip."""
    messaged = [
        site.location
        for site in raise_sites()
        if site.abstract and site.message is not None
    ]
    assert not messaged, messaged
