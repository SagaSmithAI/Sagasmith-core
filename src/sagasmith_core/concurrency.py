"""Database-enforced campaign head preconditions.

Core services use these helpers at the write boundary so optimistic concurrency
remains correct across processes, not only inside one SQLAlchemy identity map.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from sagasmith_core.models import Campaign, Character


def compare_and_swap_campaign(
    session: Session,
    campaign_id: str,
    *,
    expected_revision: int,
    expected_branch_id: str | None = None,
    values: dict[str, object] | None = None,
    advance_revision: bool = True,
) -> int:
    """Conditionally update one campaign and return its authoritative revision."""

    statement = update(Campaign).where(
        Campaign.id == campaign_id,
        Campaign.revision == int(expected_revision),
    )
    if expected_branch_id is not None:
        statement = statement.where(Campaign.active_branch_id == expected_branch_id)
    assignments = dict(values or {})
    assignments["revision"] = (
        Campaign.revision + 1 if advance_revision else Campaign.revision
    )
    changed = session.execute(
        statement.values(**assignments).returning(Campaign.revision),
        execution_options={"synchronize_session": False},
    ).scalar_one_or_none()
    if changed is None:
        raise ValueError("campaign revision conflict or branch conflict")
    return int(changed)


def compare_and_swap_character(
    session: Session,
    character_id: str,
    *,
    expected_revision: int,
    values: dict[str, object],
) -> int:
    """Conditionally update one character document and advance its revision."""

    changed = session.execute(
        update(Character)
        .where(
            Character.id == character_id,
            Character.revision == int(expected_revision),
        )
        .values(**values, revision=Character.revision + 1)
        .returning(Character.revision),
        execution_options={"synchronize_session": False},
    ).scalar_one_or_none()
    if changed is None:
        raise ValueError(f"character revision conflict: {character_id}")
    return int(changed)
