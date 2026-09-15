"""Registry for game-system extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable, Protocol

from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.parsing import MarkdownHierarchyParser


class SheetValidator(Protocol):
    def __call__(self, sheet: dict[str, Any]) -> dict[str, Any]: ...


class RuleParser(Protocol):
    def parse(self, content: str) -> Any: ...


class ModuleParser(RuleParser, Protocol):
    def document_metadata(self, content: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SystemDefinition:
    id: str
    display_name: str
    character_types: tuple[str, ...] = ("pc", "npc")
    campaign_defaults: dict[str, Any] = field(default_factory=dict)
    validate_sheet: SheetValidator | None = None
    rule_parser_factory: Callable[[], RuleParser] = MarkdownHierarchyParser
    module_parser_factory: Callable[[], ModuleParser] = MarkdownModuleParser
    protocol_version: int = 1
    implementation_version: str = "1"
    sheet_schema_version: int = 1
    capabilities: tuple[str, ...] = ()
    migrate_sheet: Callable[[dict[str, Any], int], dict[str, Any]] | None = None


class SystemRegistry:
    def __init__(self) -> None:
        self._systems: dict[str, SystemDefinition] = {}
        self._origins: dict[str, str] = {}

    def register(self, definition: SystemDefinition) -> None:
        if definition.protocol_version != 1 or definition.sheet_schema_version < 1:
            raise ValueError("unsupported system protocol or sheet schema version")
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
            distribution = getattr(entry_point, "dist", None)
            distribution_name = getattr(distribution, "name", "")
            entry_value = getattr(entry_point, "value", entry_point.name)
            origin = f"{distribution_name}:{entry_value}"
            candidate = entry_point.load()
            definition = candidate() if callable(candidate) else candidate
            if not isinstance(definition, SystemDefinition):
                raise TypeError(f"{entry_point.name} did not provide a SystemDefinition")
            if definition.id not in self._systems:
                self.register(definition)
                self._origins[definition.id] = origin
                loaded.append(definition)
            elif (
                self._origins.get(definition.id) != origin
                or self._systems[definition.id] != definition
            ):
                raise ValueError(f"conflicting plugin origin or definition for {definition.id!r}")
        return loaded


registry = SystemRegistry()
