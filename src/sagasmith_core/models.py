"""System-neutral ORM models for campaigns, characters, rules, modules, and items."""

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


class GameActor(TimestampMixin, Base):
    __tablename__ = "game_actors"
    __table_args__ = (
        Index("ix_game_actor_campaign_type", "campaign_id", "actor_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(64), default="character")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    img: Mapped[str] = mapped_column(Text, default="")
    system: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prototype_token: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    derived: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class GameItem(TimestampMixin, Base):
    __tablename__ = "game_items"
    __table_args__ = (
        Index("ix_game_item_campaign_type", "campaign_id", "item_type"),
        Index("ix_game_item_actor", "campaign_id", "actor_id"),
        Index("ix_game_item_container", "campaign_id", "container_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_actors.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    container_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_type: Mapped[str] = mapped_column(String(64), default="loot")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_key: Mapped[str] = mapped_column(String(300), default="")
    img: Mapped[str] = mapped_column(Text, default="")
    system: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class GameActivity(TimestampMixin, Base):
    __tablename__ = "game_activities"
    __table_args__ = (
        Index("ix_game_activity_item", "item_id", "activity_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("game_items.id", ondelete="CASCADE"),
        index=True,
    )
    activity_type: Mapped[str] = mapped_column(String(64), default="utility")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    activation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    consumption: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    duration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    range: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    uses: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    system: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class ActiveEffect(TimestampMixin, Base):
    __tablename__ = "active_effects"
    __table_args__ = (
        Index("ix_active_effect_campaign_actor", "campaign_id", "actor_id"),
        Index("ix_active_effect_parent", "parent_type", "parent_id"),
        Index("ix_active_effect_origin", "campaign_id", "origin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    parent_type: Mapped[str] = mapped_column(String(64), default="actor")
    parent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_actors.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    origin: Mapped[str] = mapped_column(String(300), default="")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    img: Mapped[str] = mapped_column(Text, default="")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    duration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    statuses: Mapped[list[str]] = mapped_column(JSON, default=list)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class GameMessage(Base):
    __tablename__ = "game_messages"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence", name="uq_game_message_sequence"),
        Index("ix_game_message_type", "campaign_id", "message_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), default="system")
    speaker: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_actors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    item_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    activity_id: Mapped[str | None] = mapped_column(
        ForeignKey("game_activities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    rolls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    deltas: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    pending: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    narration_hints: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text, default="")
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ItemTemplate(TimestampMixin, Base):
    __tablename__ = "item_templates"
    __table_args__ = (
        UniqueConstraint("system_id", "source_key", name="uq_item_template_source"),
        Index("ix_item_template_category", "system_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="gear")
    rarity: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    weight: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ItemInstance(TimestampMixin, Base):
    __tablename__ = "item_instances"
    __table_args__ = (
        Index("ix_item_instance_campaign_owner", "campaign_id", "owner_type", "owner_id"),
        Index("ix_item_instance_container", "campaign_id", "container_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    template_id: Mapped[str | None] = mapped_column(
        ForeignKey("item_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), default="party")
    owner_id: Mapped[str] = mapped_column(String(200), default="party")
    container_id: Mapped[str | None] = mapped_column(
        ForeignKey("item_instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    equipped_slot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attunement: Mapped[str] = mapped_column(String(32), default="none")
    identified: Mapped[bool] = mapped_column(Boolean, default=True)
    charges: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    condition: Mapped[str] = mapped_column(String(64), default="normal")
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ItemLedgerEntry(Base):
    __tablename__ = "item_ledger_entries"
    __table_args__ = (
        Index("ix_item_ledger_campaign_time", "campaign_id", "created_at"),
        Index("ix_item_ledger_item", "item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    item_id: Mapped[str | None] = mapped_column(
        ForeignKey("item_instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="runtime")
    reason: Mapped[str] = mapped_column(Text, default="")
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MapScene(TimestampMixin, Base):
    __tablename__ = "map_scenes"
    __table_args__ = (
        Index("ix_map_scene_campaign", "campaign_id", "name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    grid_size: Mapped[int] = mapped_column(Integer, default=70)
    grid_units: Mapped[str] = mapped_column(String(32), default="ft")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    background: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SceneToken(TimestampMixin, Base):
    __tablename__ = "scene_tokens"
    __table_args__ = (
        Index("ix_scene_token_scene", "scene_id", "actor_type", "actor_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("map_scenes.id", ondelete="CASCADE"),
        index=True,
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    actor_type: Mapped[str] = mapped_column(String(32), default="character")
    actor_id: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=1)
    height: Mapped[int] = mapped_column(Integer, default=1)
    elevation: Mapped[int] = mapped_column(Integer, default=0)
    disposition: Mapped[str] = mapped_column(String(32), default="neutral")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    vision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_delta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SceneRegion(TimestampMixin, Base):
    __tablename__ = "scene_regions"
    __table_args__ = (
        Index("ix_scene_region_scene", "scene_id", "behavior"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("map_scenes.id", ondelete="CASCADE"),
        index=True,
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    shape: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    behavior: Mapped[str] = mapped_column(String(64), default="area")
    origin_activity_id: Mapped[str] = mapped_column(String(200), default="")
    attached_token_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_tokens.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    duration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
        UniqueConstraint(
            "campaign_id",
            "scope_id",
            "scene_id",
            name="uq_scene_progress",
        ),
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
    scope_id: Mapped[str] = mapped_column(String(200), default="party", index=True)
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
