"""Small stable contracts for application owners; infrastructure stays optional."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .execution import CommandExecutor

from .command_contracts import CommandContext, CommandResult, NoOp
from .identity_contracts import IdempotencyIdentity, IdempotencyWrite
from .state_contracts import ActorKnowledgeTransfer, CharacterStateUpdate
from .system_contracts import ModuleParser, RuleParser, SheetValidator, SystemDefinition
from .timeline import DecisionBase
from .work import UnitOfWork

__all__ = [
    "CommandContext",
    "CommandExecutor",
    "CommandResult",
    "NoOp",
    "DecisionBase",
    "UnitOfWork",
    "IdempotencyIdentity",
    "IdempotencyWrite",
    "ActorKnowledgeTransfer",
    "CharacterStateUpdate",
    "ModuleParser",
    "RuleParser",
    "SheetValidator",
    "SystemDefinition",
]


def __getattr__(name):
    if name == "CommandExecutor":
        from .execution import CommandExecutor

        return CommandExecutor
    raise AttributeError(name)
