"""Registry for game-system extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable, Protocol

from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.parsing import MarkdownHierarchyParser


class SheetValidator(Protocol):
    def __call__(self, sheet: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SystemDefinition:
    id: str
    display_name: str
    character_types: tuple[str, ...] = ("pc", "npc")
    campaign_defaults: dict[str, Any] = field(default_factory=dict)
    validate_sheet: SheetValidator | None = None
    rule_parser_factory: Callable[[], MarkdownHierarchyParser] = MarkdownHierarchyParser
    module_parser_factory: Callable[[], MarkdownModuleParser] = MarkdownModuleParser


class SystemRegistry:
    def __init__(self) -> None:
        self._systems: dict[str, SystemDefinition] = {}

    def register(self, definition: SystemDefinition) -> None:
        if definition.id in self._systems:
            raise ValueError(f"system {definition.id!r} is already registered")
        self._systems[definition.id] = definition

    def get(self, system_id: str) -> SystemDefinition:
        try:
            return self._systems[system_id]
        except KeyError as exc:
            raise LookupError(f"unknown TTRPG system {system_id!r}") from exc

    def list(self) -> list[SystemDefinition]:
        return sorted(self._systems.values(), key=lambda item: item.id)

    def discover(self) -> list[SystemDefinition]:
        """Load installed system definitions from ``sagasmith.systems``."""
        loaded: list[SystemDefinition] = []
        for entry_point in entry_points(group="sagasmith.systems"):
            candidate = entry_point.load()
            definition = candidate() if callable(candidate) else candidate
            if not isinstance(definition, SystemDefinition):
                raise TypeError(f"{entry_point.name} did not provide a SystemDefinition")
            if definition.id not in self._systems:
                self.register(definition)
                loaded.append(definition)
        return loaded


registry = SystemRegistry()
