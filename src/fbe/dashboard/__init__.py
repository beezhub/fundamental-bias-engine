"""Single-file HTML dashboard for a run of the engine.

The dashboard is the phone-facing view of the same `fbe.types.BiasReport` the
Markdown report renders. It is one self-contained file with no external assets,
built to be published and opened away from the desk. See
`fbe.dashboard.build` for the publishing constraints and the layout.
"""

from __future__ import annotations

__all__ = ["build_dashboard"]


def __getattr__(name: str) -> object:
    """Expose `fbe.dashboard.build.build_dashboard` without importing eagerly.

    Importing the builder at package import time would drag jinja2 into every
    ``import fbe.dashboard``, including the ones that only want the constants.

    Args:
        name: Attribute being looked up.

    Returns:
        The requested attribute.

    Raises:
        AttributeError: When the name is not exported by this package.
    """
    if name == "build_dashboard":
        from fbe.dashboard.build import build_dashboard

        return build_dashboard
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
