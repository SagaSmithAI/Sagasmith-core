"""One decision, identity and result contract for application-owned commands."""

from typing import Any, Callable

from sagasmith_core.command_contracts import CommandContext, CommandResult, NoOp
from sagasmith_core.database import Database, UnitOfWork
from sagasmith_core.idempotency import IdempotencyService
from sagasmith_core.models import Campaign


class CommandExecutor:
    """Persist an exact result in the same UoW as its explicit service calls.

    The caller authenticates the principal before invoking this storage boundary.
    Fresh authorization nonces are deliberately absent from the business identity.
    """

    def __init__(
        self, database: Database, *, authorize: Callable[[UnitOfWork, CommandContext], None]
    ):
        self.database = database
        self.authorize = authorize

    def execute(
        self, context: CommandContext, payload: Any, apply: Callable[[UnitOfWork], dict[str, Any]]
    ) -> CommandResult:
        with self.database.unit_of_work(immediate=True) as work:
            return self.execute_in_work(work, context, payload, apply)

    def execute_in_work(
        self,
        work: UnitOfWork,
        context: CommandContext,
        payload: Any,
        apply: Callable[[UnitOfWork], dict[str, Any]],
    ) -> CommandResult:
        with self.database.operation(work) as session:
            self.authorize(work, context)
            identity = context.identity()
            identity.scope()
            # Replays may have an old revision, but never an old checkout identity.
            campaign = session.get(Campaign, context.decision.campaign_id, populate_existing=True)
            if campaign is None or (campaign.active_branch_id, campaign.timeline_epoch) != (
                context.decision.branch_id,
                context.decision.timeline_epoch,
            ):
                raise ValueError("stale command timeline")
            receipts = IdempotencyService(self.database)
            replay = receipts.lookup_in_session(
                session, identity.scope(), context.idempotency_key, payload
            )
            if replay is not None:
                return CommandResult("replayed", replay.response or {})
            context.decision.require_current(self.database, work)
            before_writes = work.write_count
            response = apply(work)
            session.flush()
            noop = isinstance(response, NoOp)
            if noop:
                if work.write_count != before_writes:
                    raise ValueError("noop handler attempted a database write")
                response = response.response
            if not isinstance(response, dict):
                raise TypeError("command response must be a JSON object")
            receipts.remember_identity_in_work(
                work, identity, context.idempotency_key, payload, response
            )
            return CommandResult("noop" if noop else "applied", response)
