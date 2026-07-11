"""
JARVIS AI OS — Dependency Container
=====================================
Lightweight IoC container. No magic reflection — explicit registration only.

Supports:
  - Singleton scoping (default)
  - Transient scoping (new instance per resolve)
  - Factory registration
  - Lazy initialization
  - Circular dependency detection
  - Thread-safe resolution
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Type, TypeVar

from observability.logging.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


class Scope(Enum):
    SINGLETON = auto()  # one instance for the lifetime of the container
    TRANSIENT = auto()  # new instance on every resolve


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


@dataclass
class Registration:
    name: str
    scope: Scope
    factory: Callable[[], Any]
    instance: Any = None  # cached singleton
    resolved: bool = False


# ---------------------------------------------------------------------------
# DependencyContainer
# ---------------------------------------------------------------------------


class DependencyContainer:
    """
    Explicit IoC container.

    Usage:
        container = DependencyContainer()

        # Register a singleton
        container.register_singleton("event_bus", lambda: EventBus())

        # Register a transient
        container.register_transient("request_context", lambda: RequestContext())

        # Register with pre-built instance
        container.register_instance("config", config_manager)

        # Resolve
        bus = container.resolve("event_bus")

        # Typed resolve
        bus = container.resolve_as("event_bus", EventBus)
    """

    _instance: "DependencyContainer | None" = None
    _class_lock = threading.Lock()

    def __new__(cls) -> "DependencyContainer":
        with cls._class_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._registrations: dict[str, Registration] = {}
                inst._resolve_lock = threading.RLock()
                inst._resolving: set[str] = set()  # cycle detection
                cls._instance = inst
            return cls._instance

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_singleton(
        self,
        name: str,
        factory: Callable[[], Any],
    ) -> "DependencyContainer":
        """Register a singleton — factory is called once on first resolve."""
        with self._resolve_lock:
            self._registrations[name] = Registration(
                name=name,
                scope=Scope.SINGLETON,
                factory=factory,
            )
        log.debug("Registered singleton", name=name)
        return self

    def register_transient(
        self,
        name: str,
        factory: Callable[[], Any],
    ) -> "DependencyContainer":
        """Register a transient — factory is called on every resolve."""
        with self._resolve_lock:
            self._registrations[name] = Registration(
                name=name,
                scope=Scope.TRANSIENT,
                factory=factory,
            )
        log.debug("Registered transient", name=name)
        return self

    def register_instance(self, name: str, instance: Any) -> "DependencyContainer":
        """Register a pre-built singleton instance directly."""
        with self._resolve_lock:
            self._registrations[name] = Registration(
                name=name,
                scope=Scope.SINGLETON,
                factory=lambda: instance,
                instance=instance,
                resolved=True,
            )
        log.debug("Registered instance", name=name, type=type(instance).__name__)
        return self

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, name: str) -> Any:
        with self._resolve_lock:
            reg = self._registrations.get(name)
            if reg is None:
                raise KeyError(f"DependencyContainer: '{name}' is not registered.")

            # Cycle detection
            if name in self._resolving:
                raise RuntimeError(
                    f"Circular dependency detected while resolving '{name}'. "
                    f"Resolution chain: {self._resolving}"
                )

            if reg.scope == Scope.SINGLETON and reg.resolved:
                return reg.instance

            self._resolving.add(name)
            try:
                instance = reg.factory()
            finally:
                self._resolving.discard(name)

            if reg.scope == Scope.SINGLETON:
                reg.instance = instance
                reg.resolved = True

            log.debug("Resolved dependency", name=name, scope=reg.scope.name)
            return instance

    def resolve_as(self, name: str, expected_type: Type[T]) -> T:
        """Resolve and cast — raises TypeError if wrong type."""
        instance = self.resolve(name)
        if not isinstance(instance, expected_type):
            raise TypeError(
                f"Expected {expected_type.__name__} for '{name}', "
                f"got {type(instance).__name__}"
            )
        return instance

    def try_resolve(self, name: str) -> Any | None:
        """Resolve without raising — returns None if not found."""
        try:
            return self.resolve(name)
        except (KeyError, RuntimeError):
            return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_registered(self, name: str) -> bool:
        return name in self._registrations

    def registered_names(self) -> list[str]:
        with self._resolve_lock:
            return list(self._registrations.keys())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._resolve_lock:
            return {
                name: {
                    "scope": reg.scope.name,
                    "resolved": reg.resolved,
                    "type": type(reg.instance).__name__ if reg.instance else None,
                }
                for name, reg in self._registrations.items()
            }

    def reset(self) -> None:
        """Clear all registrations. For testing only."""
        with self._resolve_lock:
            self._registrations.clear()
            log.warning("DependencyContainer reset — for testing only")


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------


def get_container() -> DependencyContainer:
    return DependencyContainer()
