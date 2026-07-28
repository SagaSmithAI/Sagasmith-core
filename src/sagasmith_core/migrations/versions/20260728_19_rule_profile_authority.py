"""Remove rule-profile projections from campaign settings."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_19"
down_revision = "20260728_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not {
        "campaigns",
        "campaign_rule_profiles",
    }.issubset(inspector.get_table_names()):
        return
    campaigns = sa.table(
        "campaigns",
        sa.column("id", sa.String()),
        sa.column("settings", sa.JSON()),
    )
    profiles = sa.table(
        "campaign_rule_profiles",
        sa.column("campaign_id", sa.String()),
    )
    profile_campaign_ids = set(
        connection.scalars(sa.select(profiles.c.campaign_id))
    )
    for campaign_id, settings in connection.execute(
        sa.select(campaigns.c.id, campaigns.c.settings)
    ):
        if campaign_id not in profile_campaign_ids:
            continue
        normalized = dict(settings or {})
        changed = "edition" in normalized or "locale" in normalized
        normalized.pop("edition", None)
        normalized.pop("locale", None)
        if changed:
            connection.execute(
                campaigns.update()
                .where(campaigns.c.id == campaign_id)
                .values(settings=normalized)
            )


def downgrade() -> None:
    # The duplicated values are intentionally not reconstructed. Their
    # authoritative copies remain in campaign_rule_profiles.
    return
