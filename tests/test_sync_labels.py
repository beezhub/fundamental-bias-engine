"""Tests for the label spec validation in `scripts/sync_labels.py`.

The script lives outside the package, so it is loaded by path rather than
imported. These tests do not touch the network: every case stops inside
`desired_labels`, which runs before the first request.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_labels.py"
SPEC = REPO_ROOT / ".github" / "labels.yml"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_labels", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_labels = _load()


def _write(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "labels.yml"
    path.write_text(yaml.safe_dump(entries))
    return path


def test_committed_spec_is_valid() -> None:
    """The spec in the repository must pass, or the sync fails part way.

    The apply loop is not atomic, so an entry the API rejects creates every
    label ahead of it and loses the rest. A 101-character description on
    `run:build-b` did exactly that: five labels created, two never reached.
    """
    labels = sync_labels.desired_labels(SPEC)
    assert len(labels) == len({entry["name"] for entry in labels})


def test_description_at_the_limit_is_accepted(tmp_path: Path) -> None:
    entries = [{"name": "edge", "color": "5319e7", "description": "x" * 100}]
    assert sync_labels.desired_labels(_write(tmp_path, entries))[0]["description"] == (
        "x" * 100
    )


def test_description_over_the_limit_is_refused(tmp_path: Path) -> None:
    entries = [{"name": "edge", "color": "5319e7", "description": "x" * 101}]
    with pytest.raises(ValueError, match="description is 101 characters, max 100"):
        sync_labels.desired_labels(_write(tmp_path, entries))


def test_colour_that_is_not_six_hex_digits_is_refused(tmp_path: Path) -> None:
    entries = [{"name": "edge", "color": "nothex", "description": "fine"}]
    with pytest.raises(ValueError, match="not six hex digits"):
        sync_labels.desired_labels(_write(tmp_path, entries))


def test_a_leading_hash_on_the_colour_is_normalised(tmp_path: Path) -> None:
    entries = [{"name": "edge", "color": "#5319E7", "description": "fine"}]
    assert sync_labels.desired_labels(_write(tmp_path, entries))[0]["color"] == "5319e7"


def test_every_violation_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """One report per run beats one fix per run.

    The API rejects an over-long description with a bare 422 naming no field,
    so finding them one at a time means one failed run per bad entry.
    """
    entries = [
        {"name": "ok", "color": "5319e7", "description": "fine"},
        {"name": "bad-desc", "color": "5319e7", "description": "x" * 101},
        {"name": "bad-colour", "color": "nothex", "description": "fine"},
    ]
    with pytest.raises(ValueError) as raised:
        sync_labels.desired_labels(_write(tmp_path, entries))
    message = str(raised.value)
    assert "2 invalid entries" in message
    assert "bad-desc" in message
    assert "bad-colour" in message
