"""`build_dashboard` and ``fbe dashboard``: one file, checked before it exists.

The page this writes is opened on a phone during a session, hours after the
terminal that produced it was closed. Nothing downstream of here can tell the
owner that it is wrong: the publishing constraints fail silently at view time,
so a violation is a blank panel rather than an error, and a blank panel four
hours later reads as "no view" rather than as a broken build.

So the order is render, check, write, and the tests below are mostly about that
order rather than about the page. Three ways it could go wrong would each look
like success on the morning it happened:

- Writing first and checking after. The file on disk is then the file the owner
  opens, and it carries nothing to say it failed.
- Writing a partial file. A crash mid-write leaves valid-looking HTML that ends
  in the middle of the matrix.
- Writing bytes other than the ones checked. The size ceiling is measured in
  UTF-8, so a page checked as a string and written under another encoding is
  checked at one size and published at another.

`render_dashboard` is the real one in every test here: this command's whole job
is to put its output on disk unaltered, and a stub would let the write path
pass while publishing something else. The report comes from
``tests/fixtures/dashboard_report.json``, the committed sidecar
`tests/test_dashboard_render.py` renders from. Nothing here reaches the network
and nothing opens a browser.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from fbe.cli import EXIT_OK, EXIT_UNUSABLE, app
from fbe.config import Config, DataConfig, RiskConfig, ScoringConfig
from fbe.dashboard.build import build_dashboard, check_constraints, render_dashboard
from fbe.report import load_report, write_report
from fbe.types import BiasReport

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_report.json"


@pytest.fixture(scope="module")
def report() -> BiasReport:
    return load_report(FIXTURE)


def reports_in(directory: Path, *runs: BiasReport) -> Path:
    """Write each run into ``directory`` the way a real `fbe report` would.

    Through `write_report` rather than by copying the fixture, so the names the
    command searches are the names the writer produces. A test that invented
    them would pass against a command looking for something else.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for run in runs:
        write_report(run, directory)
    return directory


def run_command(
    monkeypatch: pytest.MonkeyPatch,
    reports_dir: Path,
    *args: str,
) -> Result:
    """Drive ``fbe dashboard`` with the configured reports directory redirected."""
    monkeypatch.setenv("FBE_DATA_REPORTS_DIR", str(reports_dir))
    return runner.invoke(app, ["dashboard", *args])


def dashboards_in(directory: Path) -> list[Path]:
    return sorted(directory.glob("dashboard-*.html"))


def read_back(report: BiasReport, directory: Path) -> str:
    """Build the page and return what is actually on disk afterwards."""
    target = directory / "dashboard.html"
    build_dashboard(report, target)
    return target.read_text(encoding="utf-8")


# --- build_dashboard ----------------------------------------------------------


def test_the_page_is_written_and_its_path_returned(
    report: BiasReport, tmp_path: Path
) -> None:
    """The return value is what the caller prints, so it names the real file."""
    target = tmp_path / "deep" / "deeper" / "dashboard.html"

    written = build_dashboard(report, target)

    assert written == target
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_bytes_on_disk_are_the_bytes_that_were_checked(
    report: BiasReport, tmp_path: Path
) -> None:
    """The size ceiling is measured in UTF-8 and so is the write.

    A page checked as a string and written under the platform encoding is
    checked at one size and published at another, and on Windows a text write
    also rewrites every newline, which changes the byte count again.
    """
    target = tmp_path / "dashboard.html"

    build_dashboard(report, target)

    assert target.read_bytes() == render_dashboard(report).encode("utf-8")


def test_the_diff_and_the_config_reach_the_renderer(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both are optional arguments and both change the page.

    Dropping the config renders the heatmap on the packaged ladder while the
    conviction printed beside each cell came from the operator's own, and
    dropping the diff silently removes the what-changed section from a page
    that was asked for one.
    """
    seen: dict[str, object] = {}

    def watched(run: BiasReport, **kwargs: object) -> str:
        seen.update(kwargs)
        return render_dashboard(run, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("fbe.dashboard.build.render_dashboard", watched)
    build_dashboard(report, tmp_path / "dashboard.html", diff=None, config=None)

    assert seen == {"diff": None, "config": None}

    seen.clear()
    config = Config(risk=RiskConfig(), scoring=ScoringConfig(), data=DataConfig())
    build_dashboard(report, tmp_path / "dashboard.html", config=config)

    assert seen["config"] is config


def test_a_failed_rename_leaves_no_part_file_behind(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover part file in the reports directory is committed by the next
    run of anything that adds the whole tree, and it is a whole dashboard."""
    target = tmp_path / "dashboard.html"

    def refuse(self: Path, destination: object) -> Path:
        raise OSError("no room on device")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(OSError, match="no room"):
        build_dashboard(report, target)

    assert list(tmp_path.iterdir()) == []


def test_a_violation_raises_and_leaves_no_file(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing on disk is better than a page that fails at view time.

    A file written and then found unpublishable is the worst of the three
    outcomes: it is the one the owner opens, and it says nothing about having
    failed a check.
    """
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints",
        lambda html: ["first thing wrong", "second thing wrong"],
    )
    target = tmp_path / "dashboard.html"

    with pytest.raises(ValueError) as caught:
        build_dashboard(report, target)

    assert "first thing wrong" in str(caught.value)
    assert "second thing wrong" in str(caught.value)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_an_existing_page_survives_a_run_that_refuses(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yesterday's page is better than none, and a refusal must not eat it.

    Truncating the target before the check is the same defect as writing after
    it, one step earlier: the owner opens the file and finds nothing.
    """
    target = tmp_path / "dashboard.html"
    build_dashboard(report, target)
    before = target.read_bytes()
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints", lambda html: ["something wrong"]
    )

    with pytest.raises(ValueError) as caught:
        build_dashboard(report, target)

    assert target.read_bytes() == before
    # Naming it, because "nothing was written" leaves the operator guessing
    # what the phone will show, and the answer is an earlier render of the
    # same run under the same name.
    assert target.name in str(caught.value)
    assert "already holds a page" in str(caught.value)


def test_the_file_appears_whole_or_not_at_all(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write leaves valid-looking HTML that stops mid-matrix.

    The page is put in place by rename, so a reader either sees the previous
    file or the complete new one. Nothing partial is ever at the target path.
    """
    target = tmp_path / "dashboard.html"
    build_dashboard(report, target)
    whole = target.read_bytes()
    seen: list[bytes] = []

    real_replace = Path.replace

    def watched(self: Path, destination: object) -> Path:
        seen.append(target.read_bytes() if target.exists() else b"")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", watched)
    build_dashboard(report, target)

    assert seen == [whole]
    assert target.read_bytes() == whole


def test_nothing_is_left_behind_beside_the_page(
    report: BiasReport, tmp_path: Path
) -> None:
    """A leftover part file in the reports directory is committed by the next
    run of anything that adds the whole tree."""
    target = tmp_path / "dashboard.html"

    build_dashboard(report, target)

    assert [path.name for path in tmp_path.iterdir()] == ["dashboard.html"]


def test_the_file_on_disk_passes_the_publishing_checks_as_written(
    report: BiasReport, tmp_path: Path
) -> None:
    """Read back and checked again, which is where self-containment is decided.

    Asserting on the string that was rendered would only prove the check was
    called. The file is what gets published, so the file is what is checked,
    and this fails if the write ever alters what was approved.
    """
    target = tmp_path / "dashboard.html"

    build_dashboard(report, target)

    assert check_constraints(target.read_text(encoding="utf-8")) == []


def test_the_page_carries_no_reference_to_a_sibling_file(
    report: BiasReport, tmp_path: Path
) -> None:
    """Every way a document can name another file, scanned on the bytes read back.

    A relative reference resolves against wherever the file is opened from,
    which on a phone is not the reports directory, so the panel it feeds is
    simply absent. The page as it stands carries no reference of any kind, so
    what this holds is that it stays that way: a template that grows an
    ``@import`` or a background image fails here.

    Both quote styles and the CSS forms are scanned because ``check_constraints``
    reads ``src`` and ``href`` attributes, and its own docstring records that a
    ``style`` attribute's ``url()`` and a ``srcset`` are beyond it.
    """
    page = read_back(report, tmp_path)

    references = [
        *re.findall(r"""\b(?:src|srcset|href)\s*=\s*["']([^"']*)["']""", page),
        *re.findall(r"url\(\s*['\"]?([^)'\"]*)", page),
        *re.findall(r"""@import\s+["']([^"']*)["']""", page),
    ]

    assert [
        value
        for value in references
        if not value.startswith(("data:", "#", "https://"))
    ] == []


def test_the_page_that_is_checked_is_the_page_that_is_written(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One render, checked once, and those bytes are the ones that land.

    Rendering twice, or checking a page rendered without the diff and the
    config and writing one rendered with them, both publish something nobody
    looked at. The second is the live risk: an event title or a blocker string
    only appears once the diff section is there, and that is exactly the text
    that arrives from a feed.
    """
    checked: list[str] = []
    real = check_constraints
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints",
        lambda html: checked.append(html) or real(html),
    )
    target = tmp_path / "dashboard.html"
    # Bands well away from the packaged ladder, so a page rendered without this
    # config is a different page. On the defaults the two would be identical
    # and this test would pass against a build that checked one and wrote the
    # other.
    config = Config(
        risk=RiskConfig(),
        scoring=ScoringConfig(
            min_spread_low=0.30, min_spread_medium=0.60, min_spread_high=0.90
        ),
        data=DataConfig(),
    )
    plain = render_dashboard(report)
    dressed = render_dashboard(report, config=config)
    assert plain != dressed

    build_dashboard(report, target, config=config)

    assert len(checked) == 1
    assert checked[0] == dressed
    assert checked[0].encode("utf-8") == target.read_bytes()


def test_a_page_carrying_text_from_a_feed_is_written_as_utf_8(
    report: BiasReport, tmp_path: Path
) -> None:
    """The fixture is pure ASCII and a real run is not.

    Every ASCII-compatible encoding writes identical bytes for this page, so
    nothing here can tell UTF-8 from latin-1 until the page carries something
    outside it. Event titles and warnings come from a feed in the language the
    publisher used, and the size ceiling is measured in UTF-8: a page written
    under another encoding publishes a different number of bytes from the one
    that was checked, or raises partway through the write.
    """
    accented = replace(
        report, warnings=("Ifo Geschäftsklimaindex was published at 09:00 CET",)
    )
    target = tmp_path / "dashboard.html"

    build_dashboard(accented, target)
    page = render_dashboard(accented)

    assert not page.isascii()
    assert target.read_bytes() == page.encode("utf-8")


def test_the_part_file_is_made_beside_the_page_it_replaces(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.replace` is only atomic within one filesystem.

    A part file in the system temporary directory renames across a device
    boundary the moment ``data/`` is a mount or ``/tmp`` is a tmpfs, which is
    an ``OSError`` on the operator's machine and never on this one. The
    leftover is invisible here too, since nothing looks outside ``tmp_path``.
    """
    seen: dict[str, object] = {}
    real = tempfile.mkstemp

    def watched(**kwargs: object) -> tuple[int, str]:
        seen.update(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("fbe.dashboard.build.tempfile.mkstemp", watched)
    target = tmp_path / "pages" / "dashboard.html"

    build_dashboard(report, target)

    assert seen["dir"] == target.parent


# --- the command --------------------------------------------------------------


def test_the_latest_report_is_rendered_when_no_date_is_given(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is the newest run on disk, not today's date.

    Today's report does not exist until `fbe report` has run, and a command
    that insisted on it would refuse all morning. The page from the last run is
    the thing worth having on a phone in the meantime.
    """
    older = replace(report, asof=date(2026, 9, 7))
    newer = replace(report, asof=date(2026, 9, 9))
    reports = reports_in(tmp_path / "reports", older, newer)

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert dashboards_in(reports) == [reports / "dashboard-2026-09-09.html"]
    assert "dashboard-2026-09-09.html" in result.output


def test_a_named_date_renders_that_run_rather_than_the_newest(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--asof` names a run that already exists, since this command computes
    nothing and cannot produce one."""
    older = replace(report, asof=date(2026, 9, 7))
    newer = replace(report, asof=date(2026, 9, 9))
    reports = reports_in(tmp_path / "reports", older, newer)

    result = run_command(monkeypatch, reports, "--asof", "2026-09-07")

    assert result.exit_code == EXIT_OK, result.output
    assert dashboards_in(reports) == [reports / "dashboard-2026-09-07.html"]


def test_an_empty_reports_directory_names_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 1 and a sentence, rather than a stack trace or an empty page.

    An empty page would be worse than the error: it is a file, it opens, and it
    shows a run that never happened.
    """
    reports = tmp_path / "reports"
    reports.mkdir()

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert "fbe report" in result.output
    assert str(reports) in result.output
    assert dashboards_in(reports) == []


def test_a_date_with_no_report_names_the_date(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for a run that was never written is the same absence, and the
    message says which date rather than that the directory is empty."""
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports, "--asof", "2026-09-08")

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert "2026-09-08" in result.output
    assert dashboards_in(reports) == []


def test_a_page_that_fails_a_constraint_exits_non_zero_with_the_violations(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every violation, so the operator fixes them in one pass rather than
    three, and no file, so nothing unpublishable is on disk."""
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints",
        lambda html: ["<title> is missing", "body sets no background token"],
    )
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert "<title> is missing" in result.output
    assert "body sets no background token" in result.output
    assert "no violations" not in result.output
    assert dashboards_in(reports) == []


def test_a_violation_is_reported_rather_than_raised_at_the_operator(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traceback is not a message. The exception is caught and printed."""
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints", lambda html: ["something wrong"]
    )
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output


def test_out_overrides_the_default_location(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    target = tmp_path / "elsewhere" / "phone.html"

    result = run_command(monkeypatch, reports, "--out", str(target))

    assert result.exit_code == EXIT_OK, result.output
    assert target.is_file()
    assert dashboards_in(reports) == []


def test_the_default_name_carries_the_run_it_renders(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The date in the file name is the report's as-of, not today's date.

    Naming it after today would give two different runs the same name on a day
    that rendered an older one, and the file is the thing that gets published.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 7)))

    run_command(monkeypatch, reports)

    assert dashboards_in(reports) == [reports / "dashboard-2026-09-07.html"]


def test_a_run_scored_under_another_config_says_so(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one thing rendering an old run makes possible, and it is silent.

    The page is coloured on today's bands and every conviction on it was graded
    under the ones in force when the run was written. `_View.heat`'s own
    docstring says a cell coloured for one band and labelled with another
    cannot happen; rendering a report from before a config change is how it
    happens, and nothing else in the output would say so.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    monkeypatch.setenv("FBE_SCORING_MIN_SPREAD_HIGH", "2.75")

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert "config has changed" in result.output
    assert report.config_digest in result.output


def test_a_run_scored_under_this_config_says_nothing(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal morning. A warning printed every day is a warning nobody reads."""
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert "config has changed" not in result.output


def test_the_output_says_which_run_is_on_the_page(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a week-old page ships in silence.

    `fbe report` exits without writing on an empty cache, so a run of failures
    leaves the newest report where it was and this command renders it, prints
    two lines of success and exits 0. The file name carries the date and
    ``--out`` takes even that away.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 7)))

    result = run_command(monkeypatch, reports, "--out", str(tmp_path / "page.html"))

    assert "2026-09-07" in result.output
    assert f"{report.generated_at:%Y-%m-%d %H:%M}" in result.output


def test_a_relative_output_path_can_still_be_opened(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.as_uri` refuses a relative path, and both paths here can be one.

    ``--out page.html`` is what a person types, and `FBE_DATA_REPORTS_DIR` is
    stored unresolved, so the default path can be relative too. The page is
    already written when the browser is opened, so raising there reports a
    failed build for a file that is on disk and correct.
    """
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    monkeypatch.chdir(tmp_path)

    result = run_command(monkeypatch, reports, "--out", "page.html", "--open")

    assert result.exit_code == EXIT_OK, result.output
    assert opened == [(tmp_path / "page.html").resolve().as_uri()]


def test_a_browser_that_will_not_open_is_reported(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`webbrowser.open` returns False on a headless host and raises nothing."""
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports, "--open")

    assert result.exit_code == EXIT_OK, result.output
    assert "No browser could be opened" in result.output


def test_a_sidecar_that_cannot_be_read_is_refused_with_its_reason(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing and readable are different questions.

    A sidecar from another version, hand-edited or half-copied passes the first
    and fails the second, and `load_report` says exactly what is wrong with it.
    That sentence has to reach the operator as a sentence.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    (reports / "bias-2026-09-09.json").write_text('{"asof": "2026-09-09"}')

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert not isinstance(result.exception, ValueError), result.exception
    assert "bias-2026-09-09.json" in result.output
    assert dashboards_in(reports) == []


def test_an_undated_file_in_the_reports_directory_is_refused(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`latest_report` is loud about one on purpose, and loud must mean a message.

    ``bias-backup.json`` sorts after every dated name, so a newest-by-name rule
    hands it to the renderer; skipping it quietly makes the newest report
    depend on what else is in the directory.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    (reports / "bias-backup.json").write_text("{}")

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert not isinstance(result.exception, ValueError), result.exception
    assert "bias-backup.json" in result.output


def test_a_destination_that_cannot_be_written_is_refused(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full or read-only destination is a third way to exit 1.

    `docs/interfaces.md` enumerates the exits, so one that arrives as a
    traceback contradicts the page the operator was told to read.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", refuse)

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert not isinstance(result.exception, OSError), result.exception
    assert "No space left on device" in result.output


def test_no_browser_is_opened_without_the_flag(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default, and the reason nothing in this file passes ``--open``."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert opened == []


def test_the_flag_opens_the_file_that_was_written(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And opens it as a file URL, since there is no server to serve it."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    run_command(monkeypatch, reports, "--open")

    assert opened == [(reports / "dashboard-2026-09-09.html").as_uri()]


def test_nothing_is_opened_when_nothing_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is about the page, and there is no page."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    reports = tmp_path / "reports"
    reports.mkdir()

    result = run_command(monkeypatch, reports, "--open")

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert opened == []


def test_nothing_is_opened_when_the_page_was_refused(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is about the page, and the page was not written.

    Opening anyway shows a file that does not exist on a first run, and
    yesterday's page presented as today's on any later one, while the command
    exits non-zero behind it.
    """
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        "fbe.dashboard.build.check_constraints", lambda html: ["something wrong"]
    )
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports, "--open")

    assert result.exit_code == EXIT_UNUSABLE, result.output
    assert opened == []


def test_the_diff_against_the_previous_run_reaches_the_page(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--compare last` means the run before the one being rendered.

    Comparing against the newest report on disk instead would diff a
    backfilled run against a later one and print every move backwards.
    """
    older = replace(report, asof=date(2026, 9, 7))
    newer = replace(report, asof=date(2026, 9, 9))
    reports = reports_in(tmp_path / "reports", older, newer)
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["asof"] = run.asof
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    run_command(monkeypatch, reports, "--asof", "2026-09-09")

    assert seen["asof"] == date(2026, 9, 9)
    assert seen["diff"] is not None


def test_the_baseline_is_the_run_before_the_one_rendered(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendering the older of two runs has no baseline, because none precedes it.

    Taking the newest report on disk instead would diff a backfilled run
    against its own successor, and every move on the page would read backwards.
    """
    older = replace(report, asof=date(2026, 9, 7))
    newer = replace(report, asof=date(2026, 9, 9))
    reports = reports_in(tmp_path / "reports", older, newer)
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    run_command(monkeypatch, reports, "--asof", "2026-09-07")

    assert seen["diff"] is None


def test_the_diff_is_taken_from_the_baseline_to_the_run(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`diff_reports` is positional, and the two arguments are interchangeable.

    Handed the wrong way round it still renders, and a currency that gained a
    full point publishes as having lost one, with the two as-of dates swapped
    in the heading above it. Nothing raises and every number looks plausible.
    """
    weaker = replace(report, asof=date(2026, 9, 7))
    stronger = replace(
        report,
        asof=date(2026, 9, 9),
        currencies=tuple(
            replace(score, composite=score.composite + 1.0)
            if score.currency == "USD"
            else score
            for score in report.currencies
        ),
    )
    reports = reports_in(tmp_path / "reports", weaker, stronger)
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    run_command(monkeypatch, reports, "--asof", "2026-09-09")

    diff = seen["diff"]
    assert diff is not None
    assert diff.previous_asof == date(2026, 9, 7)  # type: ignore[attr-defined]
    assert diff.current_asof == date(2026, 9, 9)  # type: ignore[attr-defined]
    moved = {
        change.currency: change.delta
        for change in diff.currencies  # type: ignore[attr-defined]
    }
    assert moved["USD"] == pytest.approx(1.0)


def test_the_default_run_is_diffed_against_the_one_before_it(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `--asof` there is no date to resolve the baseline from.

    Resolving it from the date asked for, which is nothing, hands
    `latest_report` no cutoff and it returns the report being rendered. The run
    then diffs against itself, every delta is zero, and the page says nothing
    moved every morning of the year.
    """
    weaker = replace(report, asof=date(2026, 9, 7))
    stronger = replace(
        report,
        asof=date(2026, 9, 9),
        currencies=tuple(
            replace(score, composite=score.composite + 1.0)
            if score.currency == "USD"
            else score
            for score in report.currencies
        ),
    )
    reports = reports_in(tmp_path / "reports", weaker, stronger)
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    run_command(monkeypatch, reports)

    diff = seen["diff"]
    assert diff is not None
    assert diff.previous_asof == date(2026, 9, 7)  # type: ignore[attr-defined]
    moved = {
        change.currency: change.delta
        for change in diff.currencies  # type: ignore[attr-defined]
    }
    assert moved["USD"] == pytest.approx(1.0)


def test_a_report_written_today_is_the_one_rendered_by_default(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal morning, and the one case a fixed fixture date cannot reach.

    Every other test here dates its reports in the past, so a default that
    quietly excluded today would pass all of them and render yesterday on the
    one day that matters. This is the only place in this file that reads the
    real clock, and it reads it for exactly that reason.
    """
    today = date.today()
    reports = reports_in(tmp_path / "reports", replace(report, asof=today))

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert dashboards_in(reports) == [reports / f"dashboard-{today}.html"]


def test_a_baseline_given_as_a_path_is_used(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--compare` takes the same values as `report --compare`, a path included."""
    reports = reports_in(
        tmp_path / "reports",
        replace(report, asof=date(2026, 9, 7)),
        replace(report, asof=date(2026, 9, 9)),
    )
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    baseline = reports / "bias-2026-09-07.json"
    run_command(monkeypatch, reports, "--compare", str(baseline))

    diff = seen["diff"]
    assert diff is not None
    assert diff.previous_asof == date(2026, 9, 7)  # type: ignore[attr-defined]


def test_a_baseline_path_that_does_not_exist_is_refused(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, naming the option, rather than a page with no diff on it.

    Falling back to no baseline would print "no baseline report to compare
    against" on a run that asked for a specific one, which reads as a first run
    rather than as a typo.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports, "--compare", str(tmp_path / "gone.json"))

    assert result.exit_code == 2, result.output
    assert dashboards_in(reports) == []


def test_a_markdown_report_with_no_sidecar_is_refused_rather_than_opened(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar is what `load_report` reads, so it is what decides existence.

    Deciding on the Markdown accepts a pair whose sidecar is gone and then
    fails inside the read, which reaches the operator as a traceback rather
    than as the refusal the command is supposed to make.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    (reports / "bias-2026-09-09.json").unlink()
    assert (reports / "bias-2026-09-09.md").is_file()

    result = run_command(monkeypatch, reports, "--asof", "2026-09-09")

    assert result.exit_code == EXIT_UNUSABLE, result.output
    # The exit code alone does not separate a refusal from a crash: the Typer
    # runner reports an uncaught exception as exit 1 too, and prints nothing.
    assert not isinstance(result.exception, FileNotFoundError), result.exception
    assert "2026-09-09" in result.output


def test_the_refusal_names_a_date_the_operator_can_retype(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--asof` parses to a datetime, and the message is read by a person.

    "No report for 2026-09-08 00:00:00" tells them to run
    ``fbe report --asof 2026-09-08 00:00:00``, which is not a command.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports, "--asof", "2026-09-08")

    assert "2026-09-08" in result.output
    assert "00:00:00" not in result.output


def test_compare_none_renders_without_a_baseline(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = replace(report, asof=date(2026, 9, 7))
    newer = replace(report, asof=date(2026, 9, 9))
    reports = reports_in(tmp_path / "reports", older, newer)
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["diff"] = kwargs.get("diff")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    run_command(monkeypatch, reports, "--compare", "none")

    assert seen["diff"] is None


def test_the_first_run_ever_has_no_baseline_and_still_renders(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One report on disk. A missing baseline is not a failure."""
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output
    assert dashboards_in(reports) == [reports / "dashboard-2026-09-09.html"]


def test_the_config_reaches_the_renderer(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page reads the scoring bands and the blackout minutes from it.

    Rendering on the packaged defaults instead would colour the matrix by one
    ladder while the conviction beside each cell came from another.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))
    seen: dict[str, object] = {}

    def watched(run: BiasReport, out_path: Path, **kwargs: object) -> Path:
        seen["config"] = kwargs.get("config")
        out_path.write_text("<!doctype html>", encoding="utf-8")
        return out_path

    monkeypatch.setattr("fbe.cli.build_dashboard", watched)
    monkeypatch.setenv("FBE_SCORING_MIN_SPREAD_HIGH", "2.75")
    run_command(monkeypatch, reports)

    config = seen["config"]
    assert config is not None
    assert config.scoring.min_spread_high == 2.75  # type: ignore[attr-defined]


def test_the_report_is_loaded_rather_than_recomputed(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command computes nothing: no collection, no scoring, no bias layer.

    Recomputing would make the page and the committed report two readings of
    the same date taken at different times, and only one of them is the record.
    """
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("fbe dashboard recomputed the run")

    monkeypatch.setattr("fbe.cli.collect", refuse)
    monkeypatch.setattr("fbe.cli.score_currencies", refuse)
    monkeypatch.setattr("fbe.cli.build_pair_biases", refuse)

    result = run_command(monkeypatch, reports)

    assert result.exit_code == EXIT_OK, result.output


def test_the_written_page_is_the_one_the_command_reports(
    report: BiasReport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The printed path is read back and rendered again, so a command that
    printed one name and wrote another fails here."""
    reports = reports_in(tmp_path / "reports", replace(report, asof=date(2026, 9, 9)))

    result = run_command(monkeypatch, reports)

    printed = [
        word
        for word in result.output.split()
        if word.endswith("dashboard-2026-09-09.html")
    ]
    assert printed, result.output
    assert Path(printed[0]).read_bytes()
