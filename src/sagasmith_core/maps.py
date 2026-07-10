"""Campaign map scenes, tokens, and regions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, MapScene, SceneRegion, SceneToken


@dataclass(frozen=True)
class MapSceneInfo:
    id: str
    campaign_id: str
    name: str
    grid_size: int
    grid_units: str
    width: int
    height: int
    background: str
    active: bool
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SceneTokenInfo:
    id: str
    campaign_id: str
    scene_id: str
    actor_type: str
    actor_id: str
    name: str
    x: int
    y: int
    width: int
    height: int
    elevation: int
    disposition: str
    hidden: bool
    vision: dict[str, Any]
    actor_delta: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SceneRegionInfo:
    id: str
    campaign_id: str
    scene_id: str
    name: str
    shape: dict[str, Any]
    behavior: str
    origin_activity_id: str
    attached_token_id: str | None
    duration: dict[str, Any]
    metadata: dict[str, Any]


class MapService:
    """Foundry-style scene/token/region document service."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_scene(
        self,
        campaign_id: str,
        *,
        name: str,
        grid_size: int = 70,
        grid_units: str = "ft",
        width: int = 0,
        height: int = 0,
        background: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> MapSceneInfo:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            row = MapScene(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                name=name,
                grid_size=max(1, int(grid_size)),
                grid_units=grid_units or "ft",
                width=max(0, int(width)),
                height=max(0, int(height)),
                background=background,
                metadata_json=dict(metadata or {}),
            )
            session.add(row)
            session.flush()
            return self._scene_info(row)

    def list_scenes(self, campaign_id: str) -> list[MapSceneInfo]:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            rows = session.scalars(
                select(MapScene)
                .where(MapScene.campaign_id == campaign_id)
                .order_by(MapScene.name, MapScene.id)
            )
            return [self._scene_info(row) for row in rows]

    def get_scene(self, scene_id: str) -> MapSceneInfo:
        with self.database.transaction() as session:
            row = self._scene(session, scene_id)
            return self._scene_info(row)

    def create_token(
        self,
        scene_id: str,
        *,
        actor_type: str = "character",
        actor_id: str = "",
        name: str,
        x: int = 0,
        y: int = 0,
        width: int = 1,
        height: int = 1,
        elevation: int = 0,
        disposition: str = "neutral",
        hidden: bool = False,
        vision: dict[str, Any] | None = None,
        actor_delta: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SceneTokenInfo:
        with self.database.transaction() as session:
            scene = self._scene(session, scene_id)
            row = SceneToken(
                id=str(uuid.uuid4()),
                campaign_id=scene.campaign_id,
                scene_id=scene.id,
                actor_type=actor_type or "character",
                actor_id=actor_id or "",
                name=name,
                x=int(x),
                y=int(y),
                width=max(1, int(width)),
                height=max(1, int(height)),
                elevation=int(elevation),
                disposition=disposition or "neutral",
                hidden=bool(hidden),
                vision=dict(vision or {}),
                actor_delta=dict(actor_delta or {}),
                metadata_json=dict(metadata or {}),
            )
            session.add(row)
            session.flush()
            return self._token_info(row)

    def list_tokens(self, scene_id: str) -> list[SceneTokenInfo]:
        with self.database.transaction() as session:
            self._scene(session, scene_id)
            rows = session.scalars(
                select(SceneToken)
                .where(SceneToken.scene_id == scene_id)
                .order_by(SceneToken.name, SceneToken.id)
            )
            return [self._token_info(row) for row in rows]

    def get_token(self, token_id: str) -> SceneTokenInfo:
        with self.database.transaction() as session:
            return self._token_info(self._token(session, token_id))

    def update_token(
        self,
        token_id: str,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        disposition: str | None = None,
        hidden: bool | None = None,
        vision: dict[str, Any] | None = None,
        actor_delta: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SceneTokenInfo:
        with self.database.transaction() as session:
            row = self._token(session, token_id)
            if actor_type is not None:
                row.actor_type = actor_type or "character"
            if actor_id is not None:
                row.actor_id = actor_id or ""
            if name is not None:
                row.name = name
            if width is not None:
                row.width = max(1, int(width))
            if height is not None:
                row.height = max(1, int(height))
            if disposition is not None:
                row.disposition = disposition or "neutral"
            if hidden is not None:
                row.hidden = bool(hidden)
            if vision is not None:
                row.vision = dict(vision)
            if actor_delta is not None:
                row.actor_delta = dict(actor_delta)
            if metadata is not None:
                row.metadata_json = {**dict(row.metadata_json or {}), **metadata}
            session.flush()
            return self._token_info(row)

    def move_token(
        self,
        token_id: str,
        *,
        x: int,
        y: int,
        elevation: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SceneTokenInfo:
        with self.database.transaction() as session:
            row = self._token(session, token_id)
            row.x = int(x)
            row.y = int(y)
            if elevation is not None:
                row.elevation = int(elevation)
            if metadata:
                row.metadata_json = {**dict(row.metadata_json or {}), **metadata}
            session.flush()
            return self._token_info(row)

    def create_region(
        self,
        scene_id: str,
        *,
        name: str,
        shape: dict[str, Any],
        behavior: str = "area",
        origin_activity_id: str = "",
        attached_token_id: str | None = None,
        duration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SceneRegionInfo:
        with self.database.transaction() as session:
            scene = self._scene(session, scene_id)
            if attached_token_id:
                token = self._token(session, attached_token_id)
                if token.scene_id != scene.id:
                    raise ValueError("attached token must belong to the region scene")
            row = SceneRegion(
                id=str(uuid.uuid4()),
                campaign_id=scene.campaign_id,
                scene_id=scene.id,
                name=name,
                shape=dict(shape),
                behavior=behavior or "area",
                origin_activity_id=origin_activity_id or "",
                attached_token_id=attached_token_id,
                duration=dict(duration or {}),
                metadata_json=dict(metadata or {}),
            )
            session.add(row)
            session.flush()
            return self._region_info(row)

    def list_regions(self, scene_id: str) -> list[SceneRegionInfo]:
        with self.database.transaction() as session:
            self._scene(session, scene_id)
            rows = session.scalars(
                select(SceneRegion)
                .where(SceneRegion.scene_id == scene_id)
                .order_by(SceneRegion.name, SceneRegion.id)
            )
            return [self._region_info(row) for row in rows]

    def get_region(self, region_id: str) -> SceneRegionInfo:
        with self.database.transaction() as session:
            row = session.get(SceneRegion, region_id)
            if row is None:
                raise LookupError(f"region not found: {region_id}")
            return self._region_info(row)

    def update_region(
        self,
        region_id: str,
        *,
        name: str | None = None,
        shape: dict[str, Any] | None = None,
        behavior: str | None = None,
        attached_token_id: str | None = None,
        duration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SceneRegionInfo:
        with self.database.transaction() as session:
            row = session.get(SceneRegion, region_id)
            if row is None:
                raise LookupError(f"region not found: {region_id}")
            if attached_token_id is not None:
                token = self._token(session, attached_token_id)
                if token.scene_id != row.scene_id:
                    raise ValueError("attached token must belong to the region scene")
                row.attached_token_id = attached_token_id
            if name is not None:
                row.name = name
            if shape is not None:
                row.shape = dict(shape)
            if behavior is not None:
                row.behavior = behavior or "area"
            if duration is not None:
                row.duration = dict(duration)
            if metadata is not None:
                row.metadata_json = {**dict(row.metadata_json or {}), **metadata}
            session.flush()
            return self._region_info(row)

    def delete_region(self, region_id: str) -> SceneRegionInfo:
        with self.database.transaction() as session:
            row = session.get(SceneRegion, region_id)
            if row is None:
                raise LookupError(f"region not found: {region_id}")
            value = self._region_info(row)
            session.delete(row)
            return value

    @staticmethod
    def _campaign(session, campaign_id: str) -> Campaign:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    @staticmethod
    def _scene(session, scene_id: str) -> MapScene:
        row = session.get(MapScene, scene_id)
        if row is None:
            raise LookupError(f"scene not found: {scene_id}")
        return row

    @staticmethod
    def _token(session, token_id: str) -> SceneToken:
        row = session.get(SceneToken, token_id)
        if row is None:
            raise LookupError(f"token not found: {token_id}")
        return row

    @staticmethod
    def _scene_info(row: MapScene) -> MapSceneInfo:
        return MapSceneInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            name=row.name,
            grid_size=row.grid_size,
            grid_units=row.grid_units,
            width=row.width,
            height=row.height,
            background=row.background,
            active=row.active,
            metadata=dict(row.metadata_json or {}),
        )

    @staticmethod
    def _token_info(row: SceneToken) -> SceneTokenInfo:
        return SceneTokenInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            scene_id=row.scene_id,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            name=row.name,
            x=row.x,
            y=row.y,
            width=row.width,
            height=row.height,
            elevation=row.elevation,
            disposition=row.disposition,
            hidden=row.hidden,
            vision=dict(row.vision or {}),
            actor_delta=dict(row.actor_delta or {}),
            metadata=dict(row.metadata_json or {}),
        )

    @staticmethod
    def _region_info(row: SceneRegion) -> SceneRegionInfo:
        return SceneRegionInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            scene_id=row.scene_id,
            name=row.name,
            shape=dict(row.shape or {}),
            behavior=row.behavior,
            origin_activity_id=row.origin_activity_id,
            attached_token_id=row.attached_token_id,
            duration=dict(row.duration or {}),
            metadata=dict(row.metadata_json or {}),
        )
