"""Plugin protocols with lazy default parser factories."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sagasmith_core.extensions import StateExtension


def default_rule_parser():
    from sagasmith_core.parsing import MarkdownHierarchyParser

    return MarkdownHierarchyParser()


def default_module_parser():
    from sagasmith_core.modules import MarkdownModuleParser

    return MarkdownModuleParser()


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
    rule_parser_factory: Callable[[], RuleParser] = default_rule_parser
    module_parser_factory: Callable[[], ModuleParser] = default_module_parser
    protocol_version: int = 1
    implementation_version: str = "1"
    sheet_schema_version: int = 1
    capabilities: tuple[str, ...] = ()
    migrate_sheet: Callable[[dict[str, Any], int], dict[str, Any]] | None = None
    state_extensions: tuple[StateExtension, ...] = ()
