"""Campaign rule-edition and publication profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.models import Campaign, CampaignRuleProfile, Character
from sagasmith_core.rule_profile_contract import RULE_PROFILE_OWNED_SETTING_FIELDS


@dataclass(frozen=True)
class RuleProfileInfo:
    campaign_id: str
    system_id: str
    edition: str
    locale: str
    publications: tuple[str, ...]
    options: dict[str, Any]


class RuleProfileService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set(
        self,
        campaign_id: str,
        *,
        edition: str,
        locale: str = "en",
        publications: list[str] | None = None,
        options: dict[str, Any] | None = None,
        expected_campaign_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
        active_combat_option_keys: set[str] | frozenset[str] | None = None,
    ) -> RuleProfileInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            row = session.get(CampaignRuleProfile, campaign_id)
            if dict(campaign.state or {}).get("combat", {}).get("active", False):
                mutable_option_keys = set(active_combat_option_keys or ())
                if not mutable_option_keys:
                    raise ValueError("rule profile cannot change during active combat")
                if (
                    row is None
                    or row.edition != edition
                    or row.locale != locale
                    or list(row.publications or []) != list(publications or [])
                ):
                    raise ValueError(
                        "active-combat rule maintenance cannot change edition, "
                        "locale, or publications"
                    )
                current_options = dict(row.options or {})
                requested_options = dict(options or {})
                changed_option_keys = {
                    key
                    for key in set(current_options) | set(requested_options)
                    if current_options.get(key) != requested_options.get(key)
                }
                if not changed_option_keys <= mutable_option_keys:
                    raise ValueError(
                        "active-combat rule maintenance changed options outside "
                        "its explicit allowlist"
                    )
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            if (
                row is not None
                and row.edition
                and row.edition != edition
                and session.scalar(
                    select(Character.id).where(Character.campaign_id == campaign_id).limit(1)
                )
                is not None
            ):
                raise ValueError(
                    "campaign edition cannot change while characters exist; "
                    "use an explicit edition migration"
                )
            if row is None:
                row = CampaignRuleProfile(
                    campaign_id=campaign_id,
                    system_id=campaign.system_id,
                )
                session.add(row)
            row.edition = edition
            row.locale = locale
            row.publications = list(publications or [])
            row.options = dict(options or {})
            campaign.settings = {
                key: value
                for key, value in dict(campaign.settings or {}).items()
                if key not in RULE_PROFILE_OWNED_SETTING_FIELDS
            }
            campaign.revision += 1
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result={
                    "profile": result,
                    "campaign_revision": campaign.revision,
                },
            )
            return result

    def get(self, campaign_id: str) -> RuleProfileInfo | None:
        with self.database.transaction() as session:
            if session.get(Campaign, campaign_id) is None:
                raise CampaignNotFoundError(campaign_id)
            row = session.get(CampaignRuleProfile, campaign_id)
            return self._info(row) if row else None

    @staticmethod
    def _info(row: CampaignRuleProfile) -> RuleProfileInfo:
        return RuleProfileInfo(
            campaign_id=row.campaign_id,
            system_id=row.system_id,
            edition=row.edition,
            locale=row.locale,
            publications=tuple(row.publications),
            options=dict(row.options),
        )
