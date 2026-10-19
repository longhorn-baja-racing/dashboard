"""Behavioral checks for application-owned provider selection."""

import pytest

from lbr_dashboard.core import FactoryError, Registry, RegistryError


def test_registry_orders_ids_and_rejects_duplicates() -> None:
    registry = Registry[object]("importers")
    registry.register("zeta", object)
    registry.register("alpha", object)

    assert registry.identifiers() == ("alpha", "zeta")
    with pytest.raises(RegistryError, match="already registered"):
        registry.register("alpha", object)


@pytest.mark.parametrize("identifier", ["", " csv", "csv ", "  "])
def test_registry_rejects_padded_or_empty_ids(identifier: str) -> None:
    registry = Registry[object]("importers")
    with pytest.raises(RegistryError, match="must not be empty or padded"):
        registry.register(identifier, object)


def test_registry_unknown_id_is_descriptive() -> None:
    registry = Registry[object]("importers")
    with pytest.raises(KeyError, match="Unknown identifier 'json'"):
        registry.create("json")


def test_registry_wraps_factory_failures() -> None:
    def broken_factory() -> object:
        raise RuntimeError("not configured")

    registry = Registry[object]("importers")
    registry.register("broken", broken_factory)

    with pytest.raises(FactoryError, match="Factory 'broken'.*registry 'importers'.*not configured"):
        registry.create("broken")
