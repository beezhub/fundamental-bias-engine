"""Where the routine hosts' model setting is recorded, and where it is not.

Two rules meet in this module and they pull in opposite directions.

`docs/routines.md` has to record that a host's model is chosen rather than
inherited, because a host recreated from a session running something else picks
that session's model up silently and nobody finds out until a bill or a change in
behaviour makes it obvious. That is issue #48.

`CLAUDE.md` forbids naming a model in docs, with one exception for
`src/fbe/reasoning/`, which calls a language model the way the data layer calls
FRED. Naming it there describes what the software depends on to run; naming one
in `docs/routines.md` would describe how the software gets built, which is the
disclosure the rule exists to prevent.

The architect's ruling on #48 resolves the two by stating the bound **by
reference**: `docs/routines.md` points at the `model:` fields in
`.claude/agents/*.md` rather than repeating a name. That is not a compromise. The
per-agent fields are the operative setting, so a name written into `docs/` would
be a second copy of a value maintained by hand with nothing holding the two
together, which is this repository's config-drift trap in its prose form.

The check below is what keeps the ruling true, and it is deliberately written
without naming a model. It reads the tokens out of the agent definitions and
asserts none of them appears under `docs/`. A list of names inside this file
would put model names in code, which is the same rule one directory across.

Nothing here reaches the network. Every assertion reads the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / ".claude" / "agents"
DOCS = REPO / "docs"

REASONING_DOC = DOCS / "reasoning-layer.md"
"""The one file under ``docs/`` allowed to name a model.

`CLAUDE.md` draws the exception around a product dependency: the reasoning layer
calls a model the way the data layer calls FRED. The exception is a file rather
than a phrase, so this module can name the file without naming the model.
"""

MODEL_FIELD = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)

ROUTINES = DOCS / "routines.md"


def _declared_models() -> frozenset[str]:
    """Every distinct value of ``model:`` across the agent definitions.

    Read rather than listed, so this module names no model and the token set
    tracks whatever the definitions actually say. That is also what makes the
    check below enforce the pinning the ruling relies on: if a name is added to
    an agent definition and then written into `docs/`, this notices without
    anyone updating a list.
    """
    return frozenset(
        match
        for path in sorted(AGENTS.glob("*.md"))
        for match in MODEL_FIELD.findall(path.read_text())
    )


def test_the_agent_definitions_still_declare_a_model() -> None:
    """Guards every other test here from passing vacuously.

    A search for an empty token set matches nothing and proves nothing. If the
    ``model:`` fields are ever removed, this fails rather than letting the
    exclusion check below go quietly green.
    """
    assert _declared_models()


@pytest.mark.parametrize("path", sorted(DOCS.rglob("*.md")))
def test_no_model_name_appears_under_docs(path: Path) -> None:
    """`CLAUDE.md` line 137, enforced rather than remembered.

    Parametrised per file so a failure names the file rather than handing back
    one assertion about the whole tree.

    The limitation, stated rather than hidden: this catches only the names the
    agent definitions actually use. A model belonging to nothing in this
    repository could be written into `docs/` and pass. Catching that would mean
    listing names here to search for them, which is the rule this test exists to
    keep.
    """
    if path == REASONING_DOC:
        pytest.skip("the product-dependency exception, asserted separately")

    body = path.read_text().lower()
    found = sorted(model for model in _declared_models() if model.lower() in body)

    assert not found, (
        f"{path.relative_to(REPO)} names a model, which CLAUDE.md permits only "
        f"in {REASONING_DOC.relative_to(REPO)}. State the bound by reference to "
        f".claude/agents/*.md instead. Offending tokens: {found}"
    )


def test_the_reasoning_layer_exception_is_real() -> None:
    """The exception is a carve-out for a file that uses it, not a formality.

    Without this the test above could pass because no document names a model
    anywhere, and the skip would be protecting nothing. `CLAUDE.md` describes
    the reasoning layer as naming its model deliberately, so that should be
    observable.
    """
    assert REASONING_DOC.exists()
    body = REASONING_DOC.read_text().lower()

    assert any(model.lower() in body for model in _declared_models())


# --- what docs/routines.md has to say, which is criteria 1, 2 and 3 --------


def _routines() -> str:
    """The file as one line, since its prose is hard-wrapped at 80."""
    return " ".join(ROUTINES.read_text().split())


def test_routines_says_a_host_sets_its_model_explicitly() -> None:
    """Criterion 1's first half, and the half that prevents the drift.

    The reported failure is a host inheriting whatever the creating session
    happened to be running. Saying the model is set at creation is what makes
    inheritance a mistake rather than a default.
    """
    body = _routines()

    assert "model set explicitly at creation, and never inherits it" in body


def test_routines_states_the_bound_by_reference() -> None:
    """Criterion 1's second half. The bound points, it does not repeat.

    Asserting the reference rather than any name is the whole ruling: the
    operative value lives in the agent definitions, and `docs/` is not allowed a
    second copy of it.

    Asserted as the whole bound. The path appears twice in the section, so
    checking for it alone passed with the reference stripped out of the bound
    itself, which left the sentence pointing at nothing.
    """
    body = _routines()

    assert (
        "no host runs a model below the one the judgement-heavy agent "
        "definitions name in `.claude/agents/*.md`" in body
    )


def test_routines_names_where_the_per_agent_models_live() -> None:
    """Criterion 2. The two places have to be findable together.

    A reader told a host's model is set at creation needs to know what to set it
    to, and the answer is a field in a file rather than a number in prose.

    Asserted as the phrase rather than as ``"`model:`" in body``. The section
    names that field twice, so the loose form passed with one of the two
    deleted, which is how this was found.
    """
    body = _routines()

    assert "in the `model:` field of each definition's frontmatter" in body
    assert "`.claude/agents/*.md`" in body


def test_routines_says_what_to_do_when_recreating_a_host() -> None:
    """Criterion 3. The moment the drift actually happens.

    A routine's prompt can only be changed by recreating it, so recreation is
    routine rather than exceptional, and it is exactly when a model is inherited
    by accident.

    Asserted as the phrase that carries the instruction. Searching for
    "recreat" and "inherit" separately passed with the whole paragraph removed,
    because both words appear elsewhere in the section.
    """
    body = _routines()

    assert "passing the model, never relying on the caller" in body
