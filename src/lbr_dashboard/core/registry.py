"""Small deterministic registry for providers selected by the application."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Generic, TypeVar

T = TypeVar("T")
Factory = Callable[..., T]


class RegistryError(ValueError):
    """Base error for invalid registry operations."""


class FactoryError(RuntimeError):
    """Raised when a registered factory fails, with its selection context."""

    def __init__(self, registry_name: str, identifier: str, cause: Exception) -> None:
        self.registry_name = registry_name
        self.identifier = identifier
        self.cause = cause
        super().__init__(
            f"Factory '{identifier}' in registry '{registry_name}' failed: "
            f"{type(cause).__name__}: {cause}"
        )


class Registry(Generic[T]):
    """Map application-selected IDs to concrete factories.

    IDs are rejected when padded, so registration, lookup, and errors always
    use the same canonical value. Instances belong to an application; this
    class has no process-global state or plugin loading behavior.
    """

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name or name != name.strip():
            raise RegistryError("Registry name must not be empty or padded with whitespace")
        self.name = name
        self._factories: dict[str, Factory[T]] = {}

    def register(self, identifier: str, factory: Factory[T]) -> None:
        identifier = self._validate_identifier(identifier)
        if not callable(factory):
            raise RegistryError("Registry factories must be callable")
        if identifier in self._factories:
            raise RegistryError(
                f"Identifier '{identifier}' is already registered in registry '{self.name}'"
            )
        self._factories[identifier] = factory

    def get(self, identifier: str) -> Factory[T]:
        identifier = self._validate_identifier(identifier)
        try:
            return self._factories[identifier]
        except KeyError as exc:
            raise KeyError(f"Unknown identifier '{identifier}' in registry '{self.name}'") from exc

    def create(self, identifier: str, *args: object, **kwargs: object) -> T:
        factory = self.get(identifier)
        try:
            return factory(*args, **kwargs)
        except Exception as exc:
            raise FactoryError(self.name, identifier, exc) from exc

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def __contains__(self, identifier: str) -> bool:
        return self._validate_identifier(identifier) in self._factories

    def _validate_identifier(self, identifier: str) -> str:
        if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
            raise RegistryError(
                f"Registry '{self.name}' identifiers must not be empty or padded with whitespace"
            )
        return identifier

    def __iter__(self) -> Iterator[str]:
        return iter(self.identifiers())

    def __len__(self) -> int:
        return len(self._factories)
