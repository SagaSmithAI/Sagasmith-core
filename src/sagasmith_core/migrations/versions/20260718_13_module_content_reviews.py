"""Add immutable source-backed module content reviews."""

from alembic import op

from sagasmith_core.models import Base

revision = "20260718_13"
down_revision = "20260715_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Reviews are provenance evidence and are deliberately retained.
    pass
