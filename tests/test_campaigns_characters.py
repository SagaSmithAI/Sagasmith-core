import pytest
from sqlalchemy import func, select

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.models import Campaign, CampaignRuleProfile
from sagasmith_core.rule_profiles import RuleProfileService


def test_campaign_and_character_lifecycle(database) -> None:
    campaigns = CampaignService(database)
    characters = CharacterService(database)

    campaign = campaigns.create(system_id="dnd5e", name="The Long Road")
    character = characters.create(
        system_id="dnd5e",
        campaign_id=campaign.id,
        name="Mira",
        sheet={"dnd": {"level": 1, "armor_class": 14}},
    )

    assert campaigns.get(campaign.id).slug == "the-long-road"
    assert characters.get(character.id).sheet["dnd"]["armor_class"] == 14

    updated = characters.update(character.id, sheet={"dnd": {"level": 2}})
    assert updated.revision == 2
    assert characters.bind(character.id, None).campaign_id is None


def test_character_cannot_bind_across_systems(database) -> None:
    campaigns = CampaignService(database)
    characters = CharacterService(database)
    coc = campaigns.create(system_id="coc7", name="Arkham")
    hero = characters.create(system_id="dnd5e", name="Mira")

    with pytest.raises(ValueError):
        characters.bind(hero.id, coc.id)


def test_character_instantiation_overrides_template_notes(database) -> None:
    campaigns = CampaignService(database)
    characters = CharacterService(database)
    campaign = campaigns.create(system_id="dnd5e", name="Template notes")
    template = characters.create(
        system_id="dnd5e",
        name="Template monster",
        character_type="monster",
        notes={"profile": {"summary": ""}},
    )

    instance = characters.instantiate(
        template.id,
        campaign_id=campaign.id,
        notes={"profile": {"summary": "Agent-reviewed monster."}},
    )

    assert instance.notes["profile"]["summary"] == "Agent-reviewed monster."
    assert characters.get(template.id).notes["profile"]["summary"] == ""


def test_owned_campaign_initializes_rule_profile_and_replay_atomically(database) -> None:
    campaigns = CampaignService(database)
    arguments = {
        "system_id": "dnd5e",
        "name": "Atomic campaign",
        "principal_id": "owner",
        "idempotency_key": "create",
        "settings": {
            "edition": "2014",
            "locale": "en",
            "table_name": "Friday group",
        },
        "rule_profile": {
            "edition": "2014",
            "locale": "en",
            "publications": [],
            "options": {"core": "locked"},
        },
    }

    created = campaigns.create_owned(**arguments)
    replay = campaigns.create_owned(**arguments)

    assert replay == created
    assert created.revision == 2
    profile = RuleProfileService(database).get(created.id)
    assert profile is not None
    assert profile.edition == "2014"
    assert profile.options == {"core": "locked"}
    assert created.settings == {"table_name": "Friday group"}
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Campaign)) == 1
        assert session.scalar(select(func.count()).select_from(CampaignRuleProfile)) == 1


def test_owned_campaign_rejects_incomplete_profile_without_partial_rows(database) -> None:
    with pytest.raises(ValueError, match="rule_profile"):
        CampaignService(database).create_owned(
            system_id="dnd5e",
            name="Incomplete campaign",
            principal_id="owner",
            idempotency_key="create",
            rule_profile={"edition": "2014"},
        )

    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Campaign)) == 0
