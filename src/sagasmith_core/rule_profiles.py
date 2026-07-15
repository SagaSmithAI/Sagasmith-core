"""Campaign rule-edition and publication profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, CampaignRuleProfile


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
    ) -> RuleProfileInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            row = session.get(CampaignRuleProfile, campaign_id)
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
                **dict(campaign.settings or {}),
                "edition": edition,
                "locale": locale,
            }
            campaign.revision += 1
            session.flush()
            return self._info(row)

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
