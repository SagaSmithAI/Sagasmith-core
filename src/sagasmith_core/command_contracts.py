"""Infrastructure-independent command context and outcomes."""

from dataclasses import dataclass
from typing import Any, Literal

from sagasmith_core.identity_contracts import IdempotencyIdentity
from sagasmith_core.timeline import DecisionBase


@dataclass(frozen=True)
class CommandContext:
    decision: DecisionBase
    principal: str
    operation: str
    idempotency_key: str

    def identity(self) -> IdempotencyIdentity:
        if not self.idempotency_key.strip():
            raise ValueError("command requires an idempotency key")
        return IdempotencyIdentity(
            "branch",
            self.decision.campaign_id,
            self.decision.branch_id,
            self.decision.timeline_epoch,
            self.principal,
            self.operation,
        )


@dataclass(frozen=True)
class CommandResult:
    status: Literal["applied", "replayed", "noop"]
    response: dict[str, Any]


@dataclass(frozen=True)
class NoOp:
    response: dict[str, Any]
