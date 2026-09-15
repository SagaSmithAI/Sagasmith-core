"""Infrastructure-independent business identities."""

from dataclasses import asdict, dataclass
from typing import Any, Callable

from sagasmith_core.integrity import json_sha256 as request_hash


@dataclass(frozen=True)
class IdempotencyIdentity:
    scope_type: str
    campaign_id: str | None
    branch_id: str | None
    timeline_epoch: int | None
    principal: str
    operation: str

    def scope(self) -> str:
        if self.scope_type not in {"campaign", "branch", "content"}:
            raise ValueError("unknown idempotency scope type")
        if not self.principal or not self.operation:
            raise ValueError("idempotency identity requires principal and operation")
        if self.scope_type == "branch" and (not self.campaign_id or not self.branch_id):
            raise ValueError("branch idempotency requires campaign and branch")
        if self.scope_type == "branch" and (
            not isinstance(self.timeline_epoch, int) or self.timeline_epoch < 0
        ):
            raise ValueError("branch idempotency requires a timeline epoch")
        if self.scope_type == "campaign" and not self.campaign_id:
            raise ValueError("campaign idempotency requires a campaign")
        if self.scope_type != "branch" and (
            self.branch_id is not None or self.timeline_epoch is not None
        ):
            raise ValueError("only branch identities may bind a checkout")
        return "identity:" + request_hash(asdict(self))


@dataclass(frozen=True)
class IdempotencyWrite:
    """Persist an exact public replay response with its owning transaction."""

    scope: str
    payload: Any
    response: dict[str, Any] | Callable[[Any], dict[str, Any]]
