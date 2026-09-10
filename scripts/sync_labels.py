"""Bring the repository's issue labels into line with `.github/labels.yml`.

Labels applied when an issue is created are auto-created by the GitHub API with
a default grey, so a repository whose backlog was filed by an agent ends up with
every label the same colour and no descriptions. Colour is the only thing that
makes a board scannable, so this brings them back.

The script never deletes. Removing a label removes it from every issue that
carries it, which is a destructive act disguised as tidying, so pruning is
deliberate, and not implemented here.

Run it from CI, or locally with a token that can write issues:

    GITHUB_TOKEN=... python3 scripts/sync_labels.py --owner OWNER --repo REPO
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

API = "https://api.github.com"
DEFAULT_SPEC = Path(__file__).resolve().parents[1] / ".github" / "labels.yml"


def _request(
    method: str, url: str, token: str, payload: dict[str, Any] | None = None
) -> Any:
    """Call the GitHub API and return the decoded body.

    Raises on any non-success status rather than returning a partial result. A
    label sync that half worked and reported success is worse than one that
    stopped, because the half that failed is invisible on the board.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


def existing_labels(owner: str, repo: str, token: str) -> dict[str, dict[str, Any]]:
    """Return every label currently on the repository, keyed by name."""
    found: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        url = f"{API}/repos/{owner}/{repo}/labels?per_page=100&page={page}"
        batch = _request("GET", url, token)
        if not batch:
            return found
        for label in batch:
            found[label["name"]] = label
        page += 1


def desired_labels(spec: Path) -> list[dict[str, str]]:
    """Read the intended labels, normalising colours to bare lowercase hex."""
    entries = yaml.safe_load(spec.read_text())
    out: list[dict[str, str]] = []
    for entry in entries:
        out.append(
            {
                "name": entry["name"],
                "color": str(entry["color"]).lstrip("#").lower(),
                "description": entry.get("description", ""),
            }
        )
    return out


def sync(owner: str, repo: str, token: str, spec: Path, *, apply: bool) -> int:
    """Create or update labels so the repository matches the specification.

    Returns the number of labels that differed, so a dry run can be used as a
    check: a non-zero result means the board does not match the file.
    """
    current = existing_labels(owner, repo, token)
    changed = 0
    for want in desired_labels(spec):
        name = want["name"]
        have = current.get(name)
        if have is None:
            changed += 1
            print(f"create  {name:24} #{want['color']}")
            if apply:
                _request("POST", f"{API}/repos/{owner}/{repo}/labels", token, want)
        elif (
            have.get("color", "").lower() != want["color"]
            or (have.get("description") or "") != want["description"]
        ):
            changed += 1
            print(f"update  {name:24} #{have.get('color')} -> #{want['color']}")
            if apply:
                quoted = urllib.parse.quote(name, safe="")
                _request(
                    "PATCH",
                    f"{API}/repos/{owner}/{repo}/labels/{quoted}",
                    token,
                    want,
                )
        else:
            print(f"ok      {name}")
    return changed


def main() -> int:
    """Parse arguments, run the sync, and report what differed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing.",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN or GH_TOKEN must be set", file=sys.stderr)
        return 2

    changed = sync(args.owner, args.repo, token, args.spec, apply=not args.dry_run)
    verb = "would change" if args.dry_run else "changed"
    print(f"\n{changed} label(s) {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
