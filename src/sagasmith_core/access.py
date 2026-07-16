"""Campaign principals and explicit actor-level authorization."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    ActorGrant,
    Campaign,
    CampaignBranch,
    CampaignMembership,
    CampaignSnapshot,
    Character,
    Principal,
)


class AccessDeniedError(PermissionError):
    """Raised when a principal is not allowed to read or mutate campaign state."""


@dataclass(frozen=True)
class PrincipalInfo:
    id: str
    platform: str
    external_id: str
    display_name: str
    is_service: bool


@dataclass(frozen=True)
class MembershipInfo:
    campaign_id: str
    principal_id: str
    role: str


@dataclass(frozen=True)
class ActorGrantInfo:
    campaign_id: str
    principal_id: str
    actor_id: str
    can_control: bool
    can_view_private: bool


class AccessService:
    """Resolve platform identity and enforce campaign/actor visibility."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_principal(
        self,
        principal_id: str,
        *,
        platform: str = "local",
        external_id: str | None = None,
        display_name: str = "",
        is_service: bool = False,
    ) -> PrincipalInfo:
        with self.database.transaction() as session:
            row = session.get(Principal, principal_id)
            if row is None:
                row = Principal(
                    id=principal_id,
                    platform=platform,
                    external_id=external_id or principal_id,
                    display_name=display_name,
                    is_service=is_service,
                )
                session.add(row)
                session.flush()
            return self._principal(row)

    def grant_campaign(
        self, campaign_id: str, principal_id: str, *, role: str = "player"
    ) -> MembershipInfo:
        if role not in {"owner", "dm", "player", "observer"}:
            raise ValueError(f"invalid campaign role: {role}")
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            if session.get(Principal, principal_id) is None:
                raise LookupError(principal_id)
            row = session.get(
                CampaignMembership,
                {"campaign_id": campaign_id, "principal_id": principal_id},
            )
            if row is None:
                row = CampaignMembership(
                    campaign_id=campaign_id, principal_id=principal_id, role=role
                )
                session.add(row)
            else:
                row.role = role
            session.flush()
            return MembershipInfo(row.campaign_id, row.principal_id, row.role)

    def grant_actor(
        self,
        campaign_id: str,
        principal_id: str,
        actor_id: str,
        *,
        can_control: bool = False,
        can_view_private: bool = False,
    ) -> ActorGrantInfo:
        with self.database.transaction() as session:
            actor = session.get(Character, actor_id)
            if actor is None or actor.campaign_id != campaign_id:
                raise LookupError(actor_id)
            if session.get(Principal, principal_id) is None:
                raise LookupError(principal_id)
            row = session.get(
                ActorGrant,
                {
                    "campaign_id": campaign_id,
                    "principal_id": principal_id,
                    "actor_id": actor_id,
                },
            )
            if row is None:
                row = ActorGrant(
                    campaign_id=campaign_id,
                    principal_id=principal_id,
                    actor_id=actor_id,
                    can_control=can_control,
                    can_view_private=can_view_private,
                )
                session.add(row)
            else:
                row.can_control = can_control
                row.can_view_private = can_view_private
            session.flush()
            return ActorGrantInfo(
                row.campaign_id,
                row.principal_id,
                row.actor_id,
                row.can_control,
                row.can_view_private,
            )

    def membership(self, campaign_id: str, principal_id: str) -> MembershipInfo | None:
        with self.database.transaction() as session:
            row = session.get(
                CampaignMembership,
                {"campaign_id": campaign_id, "principal_id": principal_id},
            )
            return (
                None if row is None else MembershipInfo(row.campaign_id, row.principal_id, row.role)
            )

    def accessible_campaign_ids(self, principal_id: str) -> set[str]:
        """Return only campaigns explicitly granted to one principal."""
        with self.database.transaction() as session:
            return set(
                session.scalars(
                    select(CampaignMembership.campaign_id).where(
                        CampaignMembership.principal_id == principal_id
                    )
                )
            )

    def require_campaign(
        self,
        campaign_id: str,
        principal_id: str,
        *,
        roles: set[str] | None = None,
    ) -> MembershipInfo:
        membership = self.membership(campaign_id, principal_id)
        if membership is None or (roles is not None and membership.role not in roles):
            raise AccessDeniedError(
                f"principal {principal_id!r} cannot access campaign {campaign_id!r}"
            )
        return membership

    def require_actor(
        self,
        campaign_id: str,
        actor_id: str,
        principal_id: str,
        *,
        control: bool = False,
        private: bool = False,
        branch_id: str | None = None,
    ) -> ActorGrantInfo | MembershipInfo:
        membership = self.require_campaign(campaign_id, principal_id)
        # Campaign owners/DMs still need a real actor in this campaign.  A role
        # grants authority over actors; it must not turn an arbitrary identifier
        # into a readable or writable object.
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            actor_exists = False
            if campaign is not None and (
                branch_id is None or branch_id == campaign.active_branch_id
            ):
                actor = session.get(Character, actor_id)
                actor_exists = actor is not None and actor.campaign_id == campaign_id
            elif campaign is not None:
                branch = session.get(CampaignBranch, branch_id)
                if branch is not None and branch.campaign_id == campaign_id:
                    snapshot = (
                        session.get(CampaignSnapshot, branch.head_snapshot_id)
                        if branch.head_snapshot_id
                        else None
                    )
                    if snapshot is not None:
                        from sagasmith_core.snapshots import SnapshotService

                        SnapshotService._assert_integrity(session, snapshot)
                    actor_exists = snapshot is not None and any(
                        str(item.get("id")) == actor_id
                        for item in dict(snapshot.payload).get("characters", [])
                    )
            if not actor_exists:
                raise AccessDeniedError(
                    f"actor {actor_id!r} does not belong to campaign {campaign_id!r}"
                )
        if membership.role in {"owner", "dm"}:
            return membership
        with self.database.transaction() as session:
            row = session.get(
                ActorGrant,
                {"campaign_id": campaign_id, "principal_id": principal_id, "actor_id": actor_id},
            )
            if (
                row is None
                or (control and not row.can_control)
                or (private and not row.can_view_private)
            ):
                raise AccessDeniedError(
                    f"principal {principal_id!r} cannot access actor {actor_id!r}"
                )
            return ActorGrantInfo(
                row.campaign_id,
                row.principal_id,
                row.actor_id,
                row.can_control,
                row.can_view_private,
            )

    @staticmethod
    def _principal(row: Principal) -> PrincipalInfo:
        return PrincipalInfo(
            row.id, row.platform, row.external_id, row.display_name, row.is_service
        )


def default_local_principal(database: Database) -> PrincipalInfo:
    """Return the local service identity used by single-user stdio deployments."""
    return AccessService(database).ensure_principal(
        "system:local", platform="local", external_id="system:local", is_service=True
    )
