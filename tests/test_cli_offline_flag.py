"""The global ``--offline`` flag, and the ``--online`` that should not exist.

`docs/interfaces.md` and `fbe.cli._effective_config` both state the rule: passing
``--offline`` forces `fbe.config.DataConfig.offline` true, omitting it changes
nothing, and there is deliberately no way to force online from the command line.
The flag exists to make a run reproducible, and a flag that can undo that is a
flag that will.

The declaration said ``"--offline/--online"``, which registered a second flag the
documents say must not exist. Three separate wrongs came out of it. ``--online``
was advertised in ``--help``. It could not do what its name said, because the
resolver only ever acts on the true case, so the file's ``offline: true`` survived
untouched. And ``--help`` rendered ``[default: online]``, stating that the flag
decides a value it does not control.

The failure that matters is the quiet one. An operator who set ``offline: true``
while travelling, came back, saw ``--online`` in the help and ran it, would get a
cache-only run and nothing anywhere saying the flag was ignored. That is the
shape `docs/interfaces.md` names two paragraphs after the rule: "A setting that
silently does nothing is a setting the operator believes is applied." An unknown
key in the file raises. An unknown behaviour behind a real flag did not.

What is asserted here is the CLI surface: what an operator is offered, and what
happens when they take the offer. The resolution half of the rule, that passing
the flag forces offline and omitting it leaves the file alone, was already
covered in ``tests/test_config.py`` before this defect was found, which is part
of why it survived: the behaviour was right and only the declaration was wrong.
Those tests are not duplicated here.

No test reads the repository's own configuration or touches the network. These
drive ``--help`` and an unknown flag, neither of which reaches a config file.
See issue #70.
"""

from __future__ import annotations

import re

import pytest
import typer
from typer.testing import CliRunner

from fbe.cli import app

runner = CliRunner()

ANSI = re.compile(r"\x1b\[[0-9;]*m")
"""Select Graphic Rendition escapes, which is all Rich emits into help text."""


def _help_text() -> str:
    """Render ``fbe --help`` as plain text, whatever the terminal environment.

    Asserting on the raw output is not safe, and the way it fails is the
    dangerous direction. GitHub Actions exports ``FORCE_COLOR``, so Rich styles
    the help and an option name arrives split by escape sequences: ``--offline``
    stops being a substring of the output. A positive assertion then fails
    loudly, which is how this was caught, but a negative one such as
    "``--online`` is absent" passes for entirely the wrong reason.

    So the escapes are stripped and the whitespace collapsed, the second because
    Rich wraps to the terminal width and a token split across two lines would
    hide from a negative assertion just as effectively as a colour code.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    return " ".join(ANSI.sub("", result.output).split())


EXIT_USAGE = 2
"""Usage error, per the exit-code table in `docs/interfaces.md`.

Named rather than inlined because 2 also reads as an ordinary failure count in a
test, and this is the one place the number is part of a documented contract.
"""


# --- what an operator sees, which is criteria 2, 3 and 5 -------------------


def test_help_offers_offline() -> None:
    """The flag that should exist still does.

    Stated separately from the one below so that a change deleting the flag
    outright fails here rather than passing the "no --online" test vacuously.
    """
    assert "--offline" in _help_text()


def test_help_does_not_offer_online() -> None:
    """Criterion 2. Advertising it is what sends an operator to use it.

    ``--online`` is matched with its leading dashes: the help text legitimately
    contains the word "online" inside ``[default: online]`` before the fix, and
    the point here is the flag rather than the prose.
    """
    assert "--online" not in _help_text()


def test_help_does_not_claim_the_flag_decides_the_default() -> None:
    """Criterion 3. The rendered default was a false statement about provenance.

    ``[default: online]`` says the value comes from the flag's absence. It comes
    from the file or the built-in defaults, which is exactly what
    `docs/interfaces.md` says and exactly what an operator reading the help would
    have concluded was untrue.
    """
    assert "[default: online]" not in _help_text()


def test_online_is_rejected_as_a_usage_error() -> None:
    """Criteria 4 and 5. The flag must not be quietly accepted.

    Exit 2 is the usage-error code in `docs/interfaces.md`'s exit table. Before
    the fix this exits 1: the flag parses, ``doctor`` runs, and it fails later
    for its own reasons, which is worse than exit 0 would have been. The operator
    sees a plausible failure and no indication the flag did nothing.
    """
    result = runner.invoke(app, ["--online", "doctor"])

    assert result.exit_code == EXIT_USAGE


@pytest.mark.parametrize("command", ["doctor", "refresh", "score"])
def test_online_is_rejected_before_any_command(command: str) -> None:
    """The rejection belongs to the parser, so it cannot depend on the command.

    A guard implemented inside one command would pass the test above and leave
    the flag working everywhere else.
    """
    result = runner.invoke(app, ["--online", command])

    assert result.exit_code == EXIT_USAGE


# --- the declaration itself, which is criterion 1 --------------------------


def test_the_option_declares_no_secondary_name() -> None:
    """Criterion 1, read off the parser rather than the source text.

    Checking the command's own parameters means this cannot be satisfied by a
    comment or by a string that merely looks right. Typer stores a boolean pair
    as two opts on one parameter, so the assertion is on their count and content.
    """
    command = typer.main.get_command(app)
    offline = next(p for p in command.params if p.name == "offline")

    assert offline.opts == ["--offline"]
    assert not offline.secondary_opts
