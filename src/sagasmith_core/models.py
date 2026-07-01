"""System-neutral ORM models for campaigns, characters, rules, and modules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("system_id", "slug", name="uq_campaign_system_slug"),
        Index("ix_campaign_system_status", "system_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    description: Mapped[str] = mapped_column(Text, default="")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Character(TimestampMixin, Base):
    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "name",
            name="uq_character_campaign_name",
        ),
        Index("ix_character_system_type", "system_id", "character_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    character_type: Mapped[str] = mapped_column(String(32), default="pc")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    player_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    sheet: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class RuleSource(TimestampMixin, Base):
    __tablename__ = "rule_sources"
    __table_args__ = (
        UniqueConstraint("system_id", "source_key", name="uq_rule_source_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    locale: Mapped[str] = mapped_column(String(32), default="en")
    edition: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(100), default="")
    publication_id: Mapped[str] = mapped_column(String(200), default="")
    authority: Mapped[str] = mapped_column(String(32), default="primary")
    canonical_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("rule_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RuleSection(Base):
    __tablename__ = "rule_sections"
    __table_args__ = (
        Index("ix_rule_section_source_order", "source_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("rule_sources.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("rule_sections.id", ondelete="CASCADE"),
        nullable=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(500), default="")
    path: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text, default="")
    start_offset: Mapped[int] = mapped_column(Integer, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, default=0)


class RuleChunk(Base):
    __tablename__ = "rule_chunks"
    __table_args__ = (
        Index("ix_rule_chunk_source_order", "source_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("rule_sources.id", ondelete="CASCADE"),
        index=True,
    )
    section_id: Mapped[str] = mapped_column(
        ForeignKey("rule_sections.id", ondelete="CASCADE"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleSource(TimestampMixin, Base):
    __tablename__ = "module_sources"
    __table_args__ = (
        UniqueConstraint("campaign_id", "source_key", name="uq_module_campaign_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    parser_profile: Mapped[str] = mapped_column(String(100), default="generic")
    parser_version: Mapped[str] = mapped_column(String(32), default="1")
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleChapter(Base):
    __tablename__ = "module_chapters"
    __table_args__ = (Index("ix_module_chapter_order", "module_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    module_id: Mapped[str] = mapped_column(
        ForeignKey("module_sources.id", ondelete="CASCADE"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="locked")
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleScene(Base):
    __tablename__ = "module_scenes"
    __table_args__ = (Index("ix_module_scene_order", "chapter_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    module_id: Mapped[str] = mapped_column(
        ForeignKey("module_sources.id", ondelete="CASCADE"),
        index=True,
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("module_chapters.id", ondelete="CASCADE"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    scene_type: Mapped[str] = mapped_column(String(32), default="section")
    start_line: Mapped[int] = mapped_column(Integer, default=1)
    end_line: Mapped[int] = mapped_column(Integer, default=1)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    headings: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleChunk(Base):
    __tablename__ = "module_chunks"
    __table_args__ = (Index("ix_module_chunk_order", "module_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    module_id: Mapped[str] = mapped_column(
        ForeignKey("module_sources.id", ondelete="CASCADE"),
        index=True,
    )
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("module_scenes.id", ondelete="CASCADE"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    start_line: Mapped[int] = mapped_column(Integer, default=1)
    end_line: Mapped[int] = mapped_column(Integer, default=1)
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_type: Mapped[str] = mapped_column(String(32), default="narrative")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SceneProgress(TimestampMixin, Base):
    __tablename__ = "scene_progress"
    __table_args__ = (
        UniqueConstraint("campaign_id", "scene_id", name="uq_scene_progress"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("module_scenes.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="current")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_room: Mapped[str | None] = mapped_column(String(500), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignRuleProfile(TimestampMixin, Base):
    __tablename__ = "campaign_rule_profiles"

    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        primary_key=True,
    )
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    edition: Mapped[str] = mapped_column(String(64), default="")
    locale: Mapped[str] = mapped_column(String(32), default="en")
    publications: Mapped[list[str]] = mapped_column(JSON, default=list)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignEvent(Base):
    __tablename__ = "campaign_events"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence", name="uq_campaign_event_sequence"),
        Index("ix_campaign_event_type", "campaign_id", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), default="narrative")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StateRevision(Base):
    __tablename__ = "state_revisions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence", name="uq_state_revision_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("state_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_key: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_campaign_time", "campaign_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("state_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="runtime")
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignSnapshot(Base):
    __tablename__ = "campaign_snapshots"
    __table_args__ = (
        UniqueConstraint("campaign_id", "slot", name="uq_campaign_snapshot_slot"),
        Index("ix_campaign_snapshot_head", "campaign_id", "is_head"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(300), default="")
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    recap: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_head: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignMemory(TimestampMixin, Base):
    __tablename__ = "campaign_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(64), default="fact")
    subject: Mapped[str] = mapped_column(String(300), default="")


class MemoryRevision(Base):
    __tablename__ = "memory_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_memories.id", ondelete="CASCADE"),
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VectorIndexJob(TimestampMixin, Base):
    __tablename__ = "vector_index_jobs"
    __table_args__ = (
        Index("ix_vector_job_status", "status", "created_at"),
        UniqueConstraint(
            "collection",
            "entity_id",
            "operation",
            "status",
            name="uq_vector_job_pending",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    collection: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), default="upsert")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleAsset(TimestampMixin, Base):
    __tablename__ = "module_assets"
    __table_args__ = (
        UniqueConstraint("module_id", "source_path", name="uq_module_asset_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    module_id: Mapped[str] = mapped_column(
        ForeignKey("module_sources.id", ondelete="CASCADE"),
        index=True,
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), default="text/markdown")
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
