"""Campaign management service."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService
from sagasmith_core.models import (
    Campaign,
    CampaignBranch,
    CampaignMembership,
    MutationGroup,
    Principal,
)


class CampaignNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class CampaignInfo:
    id: str
    system_id: str
    slug: str
    name: str
    status: str
    description: str
    settings: dict[str, Any]
    state: dict[str, Any]
    revision: int


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or uuid.uuid4().hex[:12]


class CampaignService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        system_id: str,
        name: str,
        slug: str | None = None,
        description: str = "",
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> CampaignInfo:
        row = Campaign(
            id=str(uuid.uuid4()),
            system_id=system_id,
            slug=slugify(slug or name),
            name=name,
            description=description,
            settings=settings or {},
            state=state or {},
        )
        with self.database.transaction() as session:
            session.add(row)
            session.flush()
            branch = CampaignBranch(
                id=str(uuid.uuid4()),
                campaign_id=row.id,
                name="main",
                is_current=True,
            )
            session.add(branch)
            session.flush()
            row.active_branch_id = branch.id
            return self._info(row)

    def create_owned(
        self,
        *,
        system_id: str,
        name: str,
        principal_id: str,
        idempotency_key: str,
        slug: str | None = None,
        description: str = "",
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> CampaignInfo:
        """Atomically create a campaign, its main branch, owner, and retry receipt."""
        payload = {
            "system_id": system_id,
            "name": name,
            "slug": slug,
            "description": description,
            "settings": settings or {},
            "state": state or {},
            "principal_id": principal_id,
        }
        scope = f"campaign-create:{principal_id}"
        idempotency = IdempotencyService(self.database)
        with self.database.transaction() as session:
            replay = idempotency.lookup_in_session(
                session, scope, idempotency_key, payload
            )
            if replay is not None and replay.response is not None:
                return CampaignInfo(**replay.response)
            principal = session.get(Principal, principal_id)
            if principal is None:
                principal = Principal(
                    id=principal_id,
                    platform="mcp",
                    external_id=principal_id,
                    display_name="",
                    is_service=False,
                )
                session.add(principal)
            row = Campaign(
                id=str(uuid.uuid4()),
                system_id=system_id,
                slug=slugify(slug or name),
                name=name,
                description=description,
                settings=settings or {},
                state=state or {},
            )
            session.add(row)
            session.flush()
            branch = CampaignBranch(
                id=str(uuid.uuid4()),
                campaign_id=row.id,
                name="main",
                is_current=True,
            )
            session.add(branch)
            session.add(
                CampaignMembership(
                    campaign_id=row.id,
                    principal_id=principal_id,
                    role="owner",
                )
            )
            session.flush()
            row.active_branch_id = branch.id
            session.flush()
            result = self._info(row)
            idempotency.remember_in_session(
                session,
                scope,
                idempotency_key,
                payload,
                result.__dict__,
                campaign_id=row.id,
            )
            return result

    def get(self, campaign_id: str) -> CampaignInfo:
        with self.database.transaction() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise CampaignNotFoundError(campaign_id)
            return self._info(row)

    def list(
        self,
        *,
        system_id: str | None = None,
        status: str | None = None,
    ) -> list[CampaignInfo]:
        statement = select(Campaign).order_by(Campaign.created_at, Campaign.id)
        if system_id:
            statement = statement.where(Campaign.system_id == system_id)
        if status:
            statement = statement.where(Campaign.status == status)
        with self.database.transaction() as session:
            return [self._info(row) for row in session.scalars(statement)]

    def update(
        self,
        campaign_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> CampaignInfo:
        with self.database.transaction() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise CampaignNotFoundError(campaign_id)
            if expected_revision is not None and row.revision != expected_revision:
                raise ValueError(f"campaign revision conflict: {campaign_id}")
            if name is not None:
                row.name = name
            if status is not None:
                row.status = status
            if description is not None:
                row.description = description
            if settings is not None:
                row.settings = settings
            if state is not None:
                row.state = state
            row.revision += 1
            session.flush()
            return self._info(row)

    def update_audited(
        self,
        campaign_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        operation: str = "campaign.update",
        actor: str = "runtime",
        branch_id: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> CampaignInfo:
        """Update campaign metadata and its audit row in one transaction."""
        from sagasmith_core.revisions import RevisionService

        with self.database.transaction() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise CampaignNotFoundError(campaign_id)
            if idempotency_key and session.scalar(
                select(MutationGroup.id).where(
                    MutationGroup.campaign_id == campaign_id,
                    MutationGroup.idempotency_key == idempotency_key,
                    MutationGroup.applied.is_(True),
                )
            ):
                raise ValueError("idempotency key already has a committed campaign mutation")
            if expected_revision is not None and row.revision != expected_revision:
                raise ValueError(f"campaign revision conflict: {campaign_id}")
            before = {
                "name": row.name,
                "status": row.status,
                "description": row.description,
                "settings": dict(row.settings),
                "state": dict(row.state),
                "revision": row.revision,
            }
            if name is not None:
                row.name = name
            if status is not None:
                row.status = status
            if description is not None:
                row.description = description
            if settings is not None:
                row.settings = settings
            if state is not None:
                row.state = state
            row.revision += 1
            session.flush()
            after = {
                "name": row.name,
                "status": row.status,
                "description": row.description,
                "settings": dict(row.settings),
                "state": dict(row.state),
                "revision": row.revision,
            }
            RevisionService(self.database).record_group_in_session(
                session,
                campaign_id,
                operation=operation,
                actor=actor,
                branch_id=branch_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                changes=[
                    {
                        "entity_type": "campaign",
                        "entity_id": campaign_id,
                        "before": before,
                        "after": after,
                    }
                ],
            )
            return self._info(row)

    def delete(self, campaign_id: str) -> None:
        with self.database.transaction() as session:
            row = session.get(Campaign, campaign_id)
            if row is None:
                raise CampaignNotFoundError(campaign_id)
            session.delete(row)

    @staticmethod
    def _info(row: Campaign) -> CampaignInfo:
        return CampaignInfo(
            id=row.id,
            system_id=row.system_id,
            slug=row.slug,
            name=row.name,
            status=row.status,
            description=row.description,
            settings=dict(row.settings),
            state=dict(row.state),
            revision=row.revision,
        )
