"""Create the general TTRPG base schema."""

from alembic import op

from sagasmith_core.migrations.schema_20260701_01 import Base

revision = "20260701_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
