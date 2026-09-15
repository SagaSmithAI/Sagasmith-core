"""Keep checkout identity outside restorable story state."""

import sqlalchemy as sa
from alembic import op

revision = "20260915_35"
down_revision = "20260915_34"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("campaigns"):
        columns = {item["name"] for item in sa.inspect(bind).get_columns("campaigns")}
        if "timeline_epoch" not in columns:
            op.add_column(
                "campaigns",
                sa.Column("timeline_epoch", sa.Integer(), nullable=False, server_default="0"),
            )


def downgrade():
    raise RuntimeError("checkout identities cannot safely be discarded")
