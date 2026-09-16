"""No commit on this branch is authored by a model, an agent or a tool.

``CLAUDE.md`` forbids model, agent and tool names anywhere in the repository,
and the contribution rules require commits to carry the repository owner's
configured identity. Every other rule about a commit concerns its message, and
every check in the four this project runs looks at file contents. The author
field is neither, so a commit could satisfy every written rule and still be
wrong in the one part of itself that cannot later be edited.

That is not hypothetical. It went unnoticed for a week and reached 122 of the
194 commits on ``main`` before anything looked, because nothing did. Those stay:
rewriting the shared history of the default branch is a worse remedy than the
problem, and #117 rules it out. This check exists to stop the next one, so it
deliberately reads only commits **ahead** of ``main`` rather than ``main``
itself.

The check is a test rather than a CI step so that it runs in the same four
commands every contributor and every scheduled lane already run before pushing,
which is the point at which a bad author is still cheap to fix by amending.

Nothing here reaches the network. It shells out to ``git`` against the working
clone and reads nothing else.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "claude",
        "anthropic",
        "chatgpt",
        "openai",
        "copilot",
        "codex",
        "cursor",
        "devin",
        "gemini",
        "bard",
        "llama",
        "bot",
        "agent",
    }
)
"""Tokens that may not appear as whole words in an author or committer name.

Matched on word boundaries and case-insensitively, which is what keeps real
people out of it: ``claude`` does not match Claudia, and ``bot`` does not match
Botha or Abbott, both of which are ordinary surnames where this project is
maintained. A whole-word match is the loosest rule that cannot libel a
contributor, and a false accusation here blocks someone's push over their own
name, so the bias is deliberate.

Model version numbers are not listed. They move every few months and a list
that needs maintaining to stay correct will silently stop being correct; the
vendor and product names above are the stable part, and ``gpt`` is handled
separately below because it is three letters and collides with too much.
"""

FORBIDDEN_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgpt[-\s]?\d", re.IGNORECASE),
    re.compile(r"\bai[-\s](assistant|agent|bot)\b", re.IGNORECASE),
)
"""Names that need more than a whole-word token to be matched safely.

``gpt`` alone is three letters and appears inside ordinary words, so it is only
forbidden when a version number follows it. ``ai`` alone is far too short to
match on and is a common syllable, so it is forbidden only in the compounds that
are unambiguously a tool.
"""

FORBIDDEN_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "anthropic.com",
        "openai.com",
        "users.noreply.github.com.invalid",
    }
)
"""Domains that identify the committer as a vendor's automation.

Matched on the domain exactly, or on any subdomain of it, so
``noreply@anthropic.com`` is caught by the domain rather than by having to guess
at every local part a vendor might use. This is the half that catches an agent
configured with a plausible human name, which the name tokens above cannot.
"""


def identity_problem(name: str, email: str) -> str | None:
    """Return why an identity is not a person's, or ``None`` if it is fine.

    Args:
        name: The ``%an`` or ``%cn`` field of a commit.
        email: The ``%ae`` or ``%ce`` field of a commit.

    Returns:
        A sentence naming what matched and which field it matched in, suitable
        for putting straight into an assertion message, or ``None`` when the
        identity looks like a person's. Returning the reason rather than a bool
        is what lets the failure message say *why* without the caller
        re-deriving it.

        An empty name or email is not a problem here. It is a differently shaped
        defect, git usually refuses it at commit time, and reporting it from
        this check would attach a misleading reason to it.
    """
    lowered_name = name.casefold()
    for token in sorted(FORBIDDEN_NAME_TOKENS):
        if re.search(rf"\b{re.escape(token)}\b", lowered_name):
            return f"the name {name!r} contains the forbidden word {token!r}"

    for pattern in FORBIDDEN_NAME_PATTERNS:
        found = pattern.search(name)
        if found:
            return f"the name {name!r} matches the forbidden pattern {found.group(0)!r}"

    domain = email.rpartition("@")[2].casefold()
    for forbidden in sorted(FORBIDDEN_EMAIL_DOMAINS):
        if domain == forbidden or domain.endswith(f".{forbidden}"):
            return f"the email {email!r} is on the forbidden domain {forbidden!r}"

    return None


def _git(*args: str) -> str:
    """Run git in the working clone and return stdout, or raise on failure."""
    finished = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout


def _base_ref() -> str | None:
    """Return a ref for the trunk to compare against, or ``None`` if there is none.

    CI checks out a shallow clone by default, and a contributor may have renamed
    or never fetched the remote. Returning ``None`` rather than guessing is what
    keeps the range check honest: it reports that it could not run instead of
    passing an empty range as though it had checked something.
    """
    for ref in ("origin/main", "main"):
        try:
            _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except subprocess.CalledProcessError:
            continue
        return ref
    return None


def _commits_ahead_of_trunk() -> list[tuple[str, str, str, str, str]]:
    """Return ``(sha, author name, author email, committer name, committer email)``.

    Only commits reachable from ``HEAD`` and not from the trunk, so the history
    this check cannot fix does not fail it. On the trunk itself the range is
    empty, which is the correct answer rather than a skipped one.
    """
    base = _base_ref()
    if base is None:
        pytest.skip(
            "no origin/main or main to compare against, so the range is unknown"
        )

    # Unit and record separators, because a name may contain anything a person's
    # name may contain, including the characters a friendlier delimiter uses.
    raw = _git("log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1e", f"{base}..HEAD")
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, author_email, committer, committer_email = record.split("\x1f")
        commits.append((sha, author, author_email, committer, committer_email))
    return commits


# --- the check fires ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("Claude", "noreply@anthropic.com"),
        ("Claude Opus", "someone@example.com"),
        ("claude", "someone@example.com"),
        ("A Person", "noreply@anthropic.com"),
        ("A Person", "assistant@openai.com"),
        ("GitHub Copilot", "copilot@example.com"),
        ("Codex", "someone@example.com"),
        ("some-bot", "someone@example.com"),
        ("Release Agent", "someone@example.com"),
        ("GPT-4", "someone@example.com"),
        ("gpt 5", "someone@example.com"),
        ("AI Assistant", "someone@example.com"),
    ],
)
def test_the_check_fires_on_an_identity_that_is_not_a_person(
    name: str, email: str
) -> None:
    """Crafted examples, so the check cannot pass by matching nothing.

    Covers both halves: a forbidden name beside an innocuous email, and an
    innocuous name beside a vendor domain. The second is the one that catches an
    agent configured to look like a person.
    """
    assert identity_problem(name, email) is not None, (name, email)


# --- the check does not fire on people ---------------------------------------


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("Boitumelo Tshehla", "49273935+B-Tshehla@users.noreply.github.com"),
        ("Boitumelo Tshehla", "boitumelotshehla@gmail.com"),
        ("Claudia Bothma", "claudia@example.com"),
        ("Jan Botha", "jan@example.com"),
        ("Elizabeth Abbott", "e.abbott@example.com"),
        ("Aiden Bardsley", "aiden@example.com"),
        ("Roberta Llamas", "roberta@example.com"),
        ("Cursorio Ltd", "billing@example.com"),
        ("Gemma Gardiner", "gemma@example.com"),
    ],
)
def test_the_check_leaves_people_alone(name: str, email: str) -> None:
    """The false-positive guard, and it is the half that costs a real person.

    Every name here contains a forbidden token as a substring: Claudia, Botha,
    Abbott, Bardsley, Llamas, Cursorio, Gemma. Whole-word matching is what
    separates them from the cases above, and a regression to substring matching
    would block these contributors from pushing under their own names.
    """
    assert identity_problem(name, email) is None, (name, email)


def test_the_owner_identity_used_by_this_repository_passes() -> None:
    """The identity `main` is supposed to carry, asserted by itself.

    Named separately from the parametrised case because this is the one that
    must never start failing: if it does, the check has become stricter than the
    project's own convention and will block every push.
    """
    assert identity_problem("Boitumelo Tshehla", "boitumelotshehla@gmail.com") is None


# --- the real range ----------------------------------------------------------


def test_no_commit_on_this_branch_is_authored_by_a_model_or_a_tool() -> None:
    """The check itself, over the commits this branch adds to the trunk.

    Reads author and committer separately because they differ whenever a commit
    is amended, rebased or applied by someone other than its author, and a
    rebase is exactly how a correct author acquires a wrong committer.
    """
    problems = []
    for (
        sha,
        author,
        author_email,
        committer,
        committer_email,
    ) in _commits_ahead_of_trunk():
        for role, name, email in (
            ("author", author, author_email),
            ("committer", committer, committer_email),
        ):
            problem = identity_problem(name, email)
            if problem is not None:
                problems.append(f"{sha[:12]} {role}: {problem}")

    assert not problems, (
        "Commits on this branch carry an identity that is not a person's:\n  "
        + "\n  ".join(problems)
        + "\n\nSet the identity and amend before pushing:\n"
        "  git config user.name 'Your Name'\n"
        "  git config user.email 'your@email'\n"
        "  git commit --amend --reset-author --no-edit\n"
        "Check the whole branch with:\n"
        "  git log --format='%an <%ae>' origin/main..HEAD"
    )
