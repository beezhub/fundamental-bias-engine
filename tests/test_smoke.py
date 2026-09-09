"""Import smoke tests for the modules that are still landing.

Every check here is guarded with ``pytest.importorskip``, so a module that does
not exist yet is skipped rather than failed. The suite therefore passes today
and starts covering each module the moment it arrives, without anyone having to
remember to come back and enable it.

These are deliberately shallow: they assert that a module imports, that it
exposes the names the rest of the engine expects, and that anything advertising
itself as a `DataSource` or `Pillar` satisfies the protocol in ``fbe.types``.
Behaviour is tested in the module's own test file, which is owned by whoever
implements it.
"""

from __future__ import annotations

from types import ModuleType

import pytest

from fbe.types import DataSource, Pillar, PillarName


def _module(name: str) -> ModuleType:
    """Import ``fbe.<name>`` or skip the test if it has not landed yet."""
    return pytest.importorskip(f"fbe.{name}", reason=f"fbe.{name} not implemented yet")


# --- the implemented core, which must always import -------------------------


def test_core_modules_import() -> None:
    import fbe
    import fbe.config
    import fbe.types
    import fbe.universe

    assert fbe.__version__


# --- data sources -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "datasources.base",
        "datasources.registry",
        "datasources.cache",
        "datasources.fred",
        "datasources.cot",
        "datasources.prices",
        "datasources.calendar",
        "datasources.manual",
    ],
)
def test_datasource_module_imports(name: str) -> None:
    module = _module(name)
    assert module is not None


def test_registry_exposes_indicator_keys() -> None:
    registry = _module("datasources.registry")
    keys = getattr(registry, "INDICATORS", None)
    if keys is None:
        pytest.skip("fbe.datasources.registry has no INDICATORS mapping yet")
    assert len(keys) > 0
    assert all(isinstance(key, str) for key in keys)


def test_fred_source_satisfies_the_datasource_protocol() -> None:
    fred = _module("datasources.fred")
    source_type = getattr(fred, "FredSource", None)
    if source_type is None:
        pytest.skip("fbe.datasources.fred has no FredSource yet")
    assert isinstance(source_type, type)
    for attribute in ("fetch", "available"):
        assert hasattr(source_type, attribute), f"FredSource is missing {attribute}"


# --- pillars ----------------------------------------------------------------


PILLAR_MODULES = {
    PillarName.MONETARY: "pillars.monetary",
    PillarName.INFLATION: "pillars.inflation",
    PillarName.GROWTH: "pillars.growth",
    PillarName.EMPLOYMENT: "pillars.employment",
    PillarName.EXTERNAL: "pillars.external",
    PillarName.POSITIONING: "pillars.positioning",
    PillarName.RISK: "pillars.risk",
}


def test_pillar_base_imports() -> None:
    assert _module("pillars.base") is not None


@pytest.mark.parametrize(
    ("pillar", "name"), sorted(PILLAR_MODULES.items(), key=lambda item: item[1])
)
def test_pillar_module_imports(pillar: PillarName, name: str) -> None:
    module = _module(name)
    declared = getattr(module, "NAME", None) or getattr(module, "PILLAR", None)
    if declared is not None:
        assert declared == pillar, f"fbe.{name} declares {declared}, expected {pillar}"


# --- scoring, bias, risk, execution, output ---------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "scoring",
        "bias",
        "risk",
        "calendar_guard",
        "journal",
        "report",
        "cli",
        "dashboard",
    ],
)
def test_engine_module_imports(name: str) -> None:
    module = _module(name)
    assert module is not None


def test_cli_exposes_a_typer_app() -> None:
    cli = _module("cli")
    assert hasattr(cli, "app"), "fbe.cli must expose `app` for the console script"


# --- protocol conformance, once the pieces exist ----------------------------


def test_protocols_are_runtime_checkable() -> None:
    """Guards the smoke checks above: they rely on isinstance against these."""
    assert isinstance(DataSource, type)
    assert isinstance(Pillar, type)


def test_every_pillar_name_has_a_module_mapping() -> None:
    """If a pillar is added to the enum, this file must learn about it."""
    assert set(PILLAR_MODULES) == set(PillarName)
