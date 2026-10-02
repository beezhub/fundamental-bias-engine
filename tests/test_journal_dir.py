"""The journal's location is a setting beside the other three data directories.

Issue #328, from proposal #303. Before it, `DataConfig` carried ``cache_dir``,
``manual_dir`` and ``reports_dir`` while the journal sat at a module constant,
so moving the data tree through config left behind the one directory holding
data no rerun can recreate. These tests pin the setting, its default, that the
commands follow it, and that a wrong one is refused rather than created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fbe import journal
from fbe.config import DATA_DIR, DataConfig, load_config
from tests.test_cli_journal_add import add, taken


def _plain_config(tmp_path: Path) -> Path:
    """A config file that sets nothing in ``data``, so the default or the
    environment decides. Never the repository's own ``config.yaml``, which holds
    whatever the owner last left there."""
    path = tmp_path / "config.yaml"
    path.write_text("data:\n  offline: true\n", encoding="utf-8")
    return path


def test_the_default_is_the_journal_directory_in_the_data_tree() -> None:
    assert DataConfig().journal_dir == DATA_DIR / "journal"


def test_the_module_default_path_is_derived_from_the_setting() -> None:
    """One source for the default location, so the two cannot drift apart."""
    assert DataConfig().journal_dir / "trades.jsonl" == journal.JOURNAL_PATH


def test_the_environment_moves_it_like_its_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FBE_DATA_JOURNAL_DIR", str(tmp_path))

    assert load_config(_plain_config(tmp_path)).data.journal_dir == tmp_path


def test_a_config_file_moves_it(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"data:\n  journal_dir: {tmp_path / 'j'}\n", encoding="utf-8")

    assert load_config(path).data.journal_dir == tmp_path / "j"


def test_a_journal_dir_that_is_a_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    occupied = tmp_path / "journal"
    occupied.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("FBE_DATA_JOURNAL_DIR", str(occupied))

    problems = load_config(_plain_config(tmp_path)).validate()

    assert any(
        "journal_dir" in problem and str(occupied) in problem for problem in problems
    )


def test_a_journal_dir_that_does_not_exist_is_not_a_config_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent is `journal add`'s to refuse, not every command's.

    `fbe score` has no reason to stop because the journal folder is missing,
    and `fbe doctor` reports it on its own line.
    """
    monkeypatch.setenv("FBE_DATA_JOURNAL_DIR", str(tmp_path / "missing"))

    problems = load_config(_plain_config(tmp_path)).validate()

    assert not any("journal_dir" in problem for problem in problems)


def test_journal_add_writes_into_the_configured_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The helper's config points ``journal_dir`` at ``tmp_path``, and nowhere else."""
    code, output, written = add(monkeypatch, tmp_path, *taken())

    assert code == 0, output
    assert written == tmp_path / "trades.jsonl"
    assert written.exists()


def test_journal_add_refuses_a_directory_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrong location is refused, never quietly started as a new journal.

    The failure this prevents: the data tree is moved, the journal setting
    points at the old place, and the next trade begins an empty file there
    while the real history sits somewhere nothing reads.
    """
    missing = tmp_path / "moved" / "journal"
    monkeypatch.setenv("FBE_DATA_JOURNAL_DIR", str(missing))

    code, output, _ = add(monkeypatch, tmp_path, *taken())

    assert code != 0
    assert str(missing) in output
    assert not missing.exists()
    assert not (tmp_path / "trades.jsonl").exists()
