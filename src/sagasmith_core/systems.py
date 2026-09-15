"""Registry for game-system extensions."""

from __future__ import annotations

from importlib.metadata import entry_points

from sagasmith_core.system_contracts import (
    ModuleParser as ModuleParser,
)
from sagasmith_core.system_contracts import (
    RuleParser as RuleParser,
)
from sagasmith_core.system_contracts import (
    SheetValidator as SheetValidator,
)
from sagasmith_core.system_contracts import (
    SystemDefinition,
)


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
