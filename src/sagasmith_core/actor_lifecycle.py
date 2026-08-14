"""Atomic creation of campaign actors and their initial authority/state."""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.characters import CharacterInfo, CharacterNotFoundError
from sagasmith_core.concurrency import compare_and_swap_campaign
from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService, request_hash
from sagasmith_core.integrity import canonical_json
from sagasmith_core.models import ActorGrant, Campaign, Character, Principal
from sagasmith_core.revisions import RevisionInfo, RevisionService


@dataclass(frozen=True)
class InitialActorGrant:
    principal_id: str
    can_control: bool = True
    can_view_private: bool = True


@dataclass(frozen=True)
class ActorLifecycleResult:
    character: CharacterInfo
    revisions: tuple[RevisionInfo, ...]
    mutation_group_id: str
    replayed: bool


class ActorLifecycleService:
    """Create one validated actor as a reversible campaign mutation.

    System packages own sheet/notes validation and optional campaign-state
    mechanics. Core only commits the explicit documents, initial grants,
    idempotency receipt, and lifecycle revision in one transaction.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        campaign_id: str,
        *,
        system_id: str,
        name: str,
        character_type: str,
        sheet: dict[str, Any],
        notes: dict[str, Any],
        principal_id: str,
        idempotency_key: str,
        player_name: str | None = None,
        summary: str = "",
        template_id: str | None = None,
        initial_grants: tuple[InitialActorGrant, ...] = (),
        campaign_state: dict[str, Any] | None = None,
        expected_campaign_revision: int | None = None,
        operation: str = "actor.lifecycle.create",
        actor: str = "runtime",
        branch_id: str | None = None,
        actor_id: str | None = None,
        idempotency_payload: dict[str, Any] | None = None,
        response_extra: dict[str, Any] | None = None,
    ) -> ActorLifecycleResult:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        grant_principals = [item.principal_id for item in initial_grants]
        if len(grant_principals) != len(set(grant_principals)):
            raise ValueError("initial actor grants must not duplicate principals")
        lifecycle_payload = {
            "campaign_id": campaign_id,
            "system_id": system_id,
            "name": name,
            "character_type": character_type,
            "player_name": player_name,
            "summary": summary,
            "sheet": copy.deepcopy(sheet),
            "notes": copy.deepcopy(notes),
            "template_id": template_id,
            "initial_grants": [asdict(item) for item in initial_grants],
            "campaign_state": copy.deepcopy(campaign_state),
            "expected_campaign_revision": expected_campaign_revision,
            "operation": operation,
            "branch_id": branch_id,
            "actor_id": actor_id,
        }
        payload = copy.deepcopy(idempotency_payload or lifecycle_payload)
        scope = f"actor-lifecycle:{campaign_id}:{principal_id}"
        idempotency = IdempotencyService(self.database)
        with self.database.transaction() as session:
            replay = idempotency.lookup_in_session(session, scope, key, payload)
            if replay is not None and replay.response is not None:
                return self._result_from_response(replay.response, replayed=True)
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if campaign.system_id != system_id:
                raise ValueError("actor and campaign must use the same system_id")
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            template = session.get(Character, template_id) if template_id else None
            if template_id and template is None:
                raise CharacterNotFoundError(template_id)
            if template is not None:
                if template.campaign_id is not None:
                    raise ValueError("only a library actor can be used as a template")
                if template.system_id != system_id:
                    raise ValueError("template and campaign must use the same system_id")
            for grant in initial_grants:
                if session.get(Principal, grant.principal_id) is None:
                    raise LookupError(grant.principal_id)

            before_campaign = {
                "state": copy.deepcopy(campaign.state),
                "revision": campaign.revision,
            }
            if campaign_state is not None:
                compare_and_swap_campaign(
                    session,
                    campaign_id,
                    expected_revision=(
                        campaign.revision
                        if expected_campaign_revision is None
                        else expected_campaign_revision
                    ),
                    expected_branch_id=branch_id or campaign.active_branch_id,
                    values={"state": copy.deepcopy(campaign_state)},
                )
                session.expire(campaign)
                session.refresh(campaign)

            row = Character(
                id=actor_id or str(uuid.uuid4()),
                system_id=system_id,
                campaign_id=campaign_id,
                template_id=template_id,
                character_type=character_type,
                name=name,
                player_name=player_name,
                summary=summary,
                sheet=copy.deepcopy(sheet),
                notes=copy.deepcopy(notes),
            )
            session.add(row)
            session.flush()
            for grant in initial_grants:
                session.add(
                    ActorGrant(
                        campaign_id=campaign_id,
                        principal_id=grant.principal_id,
                        actor_id=row.id,
                        can_control=grant.can_control,
                        can_view_private=grant.can_view_private,
                    )
                )
            session.flush()
            character = self._character_info(row)
            lifecycle_document = {
                "character": asdict(character),
                "grants": [
                    {
                        "principal_id": grant.principal_id,
                        "can_control": grant.can_control,
                        "can_view_private": grant.can_view_private,
                    }
                    for grant in initial_grants
                ],
            }
            changes: list[dict[str, Any]] = []
            if campaign_state is not None:
                changes.append(
                    {
                        "entity_type": "campaign",
                        "entity_id": campaign_id,
                        "before": before_campaign,
                        "after": {
                            "state": copy.deepcopy(campaign.state),
                            "revision": campaign.revision,
                        },
                    }
                )
            changes.append(
                {
                    "entity_type": "actor_lifecycle",
                    "entity_id": row.id,
                    "before": None,
                    "after": lifecycle_document,
                }
            )
            revisions = RevisionService(self.database).record_group_in_session(
                session,
                campaign_id,
                operation=operation,
                changes=changes,
                actor=actor,
                branch_id=branch_id,
                idempotency_key=key,
                request_hash=request_hash(payload),
            )
            mutation_group_id = str(revisions[0].mutation_group_id or "")
            if not mutation_group_id:
                raise RuntimeError("actor lifecycle mutation did not create a mutation group")
            result = ActorLifecycleResult(
                character=character,
                revisions=tuple(revisions),
                mutation_group_id=mutation_group_id,
                replayed=False,
            )
            idempotency.remember_in_session(
                session,
                scope,
                key,
                payload,
                self._response(result, extra=response_extra),
                campaign_id=campaign_id,
                mutation_group_id=mutation_group_id,
            )
            return result

    @staticmethod
    def _character_info(row: Character) -> CharacterInfo:
        return CharacterInfo(
            id=row.id,
            system_id=row.system_id,
            campaign_id=row.campaign_id,
            template_id=row.template_id,
            character_type=row.character_type,
            name=row.name,
            player_name=row.player_name,
            summary=row.summary,
            sheet=json.loads(canonical_json(row.sheet)),
            notes=json.loads(canonical_json(row.notes)),
            revision=row.revision,
        )

    @staticmethod
    def _response(
        result: ActorLifecycleResult, *, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "character": asdict(result.character),
            "revisions": [asdict(item) for item in result.revisions],
            "mutation_group_id": result.mutation_group_id,
            **copy.deepcopy(extra or {}),
        }

    @staticmethod
    def _result_from_response(
        response: dict[str, Any], *, replayed: bool
    ) -> ActorLifecycleResult:
        return ActorLifecycleResult(
            character=CharacterInfo(**dict(response["character"])),
            revisions=tuple(RevisionInfo(**dict(item)) for item in response["revisions"]),
            mutation_group_id=str(response["mutation_group_id"]),
            replayed=replayed,
        )
