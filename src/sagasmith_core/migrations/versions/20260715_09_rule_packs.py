"""Add branch-local content-addressed rule packs."""

from alembic import op

from sagasmith_core.models import Base

revision = "20260715_09"
down_revision = "20260714_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The preceding integrity migration also builds new model tables on a fresh
    # install. checkfirst keeps this revision safe for both fresh and existing DBs.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Rule locks are historical integrity records and are retained intentionally.
    pass
