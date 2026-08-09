"""Remove metadata copies of source activation state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_23"
down_revision = "20260728_22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    for table_name in ("rule_sources", "module_sources"):
        if not sa.inspect(bind).has_table(table_name):
            continue
        table = sa.Table(table_name, metadata, autoload_with=bind)
        for source_id, source_metadata in bind.execute(
            sa.select(table.c.id, table.c.metadata_json)
        ):
            if not isinstance(source_metadata, dict) or "import_state" not in source_metadata:
                continue
            normalized = dict(source_metadata)
            normalized.pop("import_state", None)
            bind.execute(
                sa.update(table).where(table.c.id == source_id).values(metadata_json=normalized)
            )


def downgrade() -> None:
    # The removed value duplicated the relational ``active`` column and cannot
    # represent staged versus retired state reliably, so it is not recreated.
    return
