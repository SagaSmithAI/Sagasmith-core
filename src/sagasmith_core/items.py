"""Campaign-level item templates, inventory instances, and ledger entries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import Campaign, Character, ItemInstance, ItemLedgerEntry, ItemTemplate


OWNER_TYPES = {"character", "party", "npc", "location", "container"}


@dataclass(frozen=True)
class ItemTemplateInfo:
    id: str
    system_id: str
    source_key: str
    name: str
    category: str
    rarity: str
    tags: list[str]
    weight: int
    value: dict[str, Any]
    rules: dict[str, Any]
    description: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ItemInfo:
    id: str
    campaign_id: str
    template_id: str | None
    name: str
    owner_type: str
    owner_id: str
    container_id: str | None
    quantity: int
    equipped_slot: str | None
    attunement: str
    identified: bool
    charges: dict[str, Any]
    condition: str
    state: dict[str, Any]


@dataclass(frozen=True)
class ItemLedgerInfo:
    id: str
    campaign_id: str
    item_id: str | None
    operation: str
    actor: str
    reason: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: str


def normalize_inventory(value: Any) -> list[dict[str, Any]]:
    """Accept legacy inventory shapes and return strict item dictionaries."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("inventory must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            item = {"name": raw, "quantity": 1}
        elif isinstance(raw, dict):
            item = dict(raw)
        else:
            raise ValueError(f"inventory item {index} must be an object or string")
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            raise ValueError(f"inventory item {index} is missing name")
        quantity = int(item.get("quantity", item.get("count", 1)) or 1)
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        normalized = {
            "name": name,
            "quantity": quantity,
            "category": str(item.get("category") or "gear"),
            "equipped_slot": item.get("equipped_slot") or item.get("slot"),
            "attunement": str(item.get("attunement") or "none"),
            "identified": bool(item.get("identified", True)),
            "charges": dict(item.get("charges") or {}),
            "condition": str(item.get("condition") or "normal"),
            "state": dict(item.get("state") or item.get("metadata") or {}),
            "template": dict(item.get("template") or {}),
        }
        result.append(normalized)
    return result


class InventoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_template(
        self,
        *,
        system_id: str,
        name: str,
        source_key: str | None = None,
        category: str = "gear",
        rarity: str = "",
        tags: list[str] | None = None,
        weight: int = 0,
        value: dict[str, Any] | None = None,
        rules: dict[str, Any] | None = None,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ItemTemplateInfo:
        source_key = source_key or f"custom:{name.lower().replace(' ', '-')}"
        with self.database.transaction() as session:
            row = session.scalar(
                select(ItemTemplate).where(
                    ItemTemplate.system_id == system_id,
                    ItemTemplate.source_key == source_key,
                )
            )
            if row is None:
                row = ItemTemplate(
                    id=str(uuid.uuid4()),
                    system_id=system_id,
                    source_key=source_key,
                    name=name,
                )
                session.add(row)
            row.name = name
            row.category = category
            row.rarity = rarity
            row.tags = list(tags or [])
            row.weight = int(weight or 0)
            row.value = dict(value or {})
            row.rules = dict(rules or {})
            row.description = description
            row.metadata_json = dict(metadata or {})
            session.flush()
            return self._template_info(row)

    def list_templates(
        self,
        *,
        system_id: str | None = None,
        category: str | None = None,
    ) -> list[ItemTemplateInfo]:
        statement = select(ItemTemplate).order_by(ItemTemplate.name, ItemTemplate.id)
        if system_id:
            statement = statement.where(ItemTemplate.system_id == system_id)
        if category:
            statement = statement.where(ItemTemplate.category == category)
        with self.database.transaction() as session:
            return [self._template_info(row) for row in session.scalars(statement)]

    def get_template(self, template_id: str) -> ItemTemplateInfo:
        with self.database.transaction() as session:
            row = session.get(ItemTemplate, template_id)
            if row is None:
                raise LookupError(template_id)
            return self._template_info(row)

    def add_item(
        self,
        *,
        campaign_id: str,
        name: str,
        template_id: str | None = None,
        owner_type: str = "party",
        owner_id: str = "party",
        container_id: str | None = None,
        quantity: int = 1,
        equipped_slot: str | None = None,
        attunement: str = "none",
        identified: bool = True,
        charges: dict[str, Any] | None = None,
        condition: str = "normal",
        state: dict[str, Any] | None = None,
        actor: str = "runtime",
        reason: str = "",
    ) -> ItemInfo:
        self._validate_owner(owner_type, owner_id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            if template_id and session.get(ItemTemplate, template_id) is None:
                raise LookupError(template_id)
            if container_id and session.get(ItemInstance, container_id) is None:
                raise LookupError(container_id)
            row = ItemInstance(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                template_id=template_id,
                name=name,
                owner_type=owner_type,
                owner_id=owner_id,
                container_id=container_id,
                quantity=max(0, int(quantity)),
                equipped_slot=equipped_slot,
                attunement=attunement,
                identified=identified,
                charges=dict(charges or {}),
                condition=condition,
                state=dict(state or {}),
            )
            session.add(row)
            session.flush()
            after = self._item_dict(row)
            self._ledger(session, campaign_id, row.id, "add", None, after, actor, reason)
            return self._item_info(row)

    def list_items(
        self,
        *,
        campaign_id: str,
        owner_type: str | None = None,
        owner_id: str | None = None,
        container_id: str | None = None,
    ) -> list[ItemInfo]:
        statement = select(ItemInstance).where(ItemInstance.campaign_id == campaign_id)
        if owner_type:
            statement = statement.where(ItemInstance.owner_type == owner_type)
        if owner_id:
            statement = statement.where(ItemInstance.owner_id == owner_id)
        if container_id:
            statement = statement.where(ItemInstance.container_id == container_id)
        statement = statement.order_by(ItemInstance.owner_type, ItemInstance.name, ItemInstance.id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            return [self._item_info(row) for row in session.scalars(statement)]

    def get_item(self, item_id: str) -> ItemInfo:
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            return self._item_info(row)

    def update_item(self, item_id: str, *, actor: str = "runtime", reason: str = "", **fields: Any) -> ItemInfo:
        allowed = {
            "name",
            "quantity",
            "equipped_slot",
            "attunement",
            "identified",
            "charges",
            "condition",
            "state",
            "container_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            before = self._item_dict(row)
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            session.flush()
            after = self._item_dict(row)
            self._ledger(session, row.campaign_id, row.id, "update", before, after, actor, reason)
            return self._item_info(row)

    def move_item(
        self,
        item_id: str,
        *,
        owner_type: str,
        owner_id: str,
        container_id: str | None = None,
        actor: str = "runtime",
        reason: str = "",
    ) -> ItemInfo:
        self._validate_owner(owner_type, owner_id)
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            before = self._item_dict(row)
            row.owner_type = owner_type
            row.owner_id = owner_id
            row.container_id = container_id
            row.equipped_slot = None
            session.flush()
            after = self._item_dict(row)
            self._ledger(session, row.campaign_id, row.id, "move", before, after, actor, reason)
            return self._item_info(row)

    def equip_item(
        self,
        item_id: str,
        *,
        slot: str | None,
        actor: str = "runtime",
        reason: str = "",
    ) -> ItemInfo:
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            before = self._item_dict(row)
            row.equipped_slot = slot
            session.flush()
            after = self._item_dict(row)
            self._ledger(
                session,
                row.campaign_id,
                row.id,
                "equip" if slot else "unequip",
                before,
                after,
                actor,
                reason,
            )
            return self._item_info(row)

    def use_item(
        self,
        item_id: str,
        *,
        quantity: int = 1,
        actor: str = "runtime",
        reason: str = "",
    ) -> ItemInfo:
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            before = self._item_dict(row)
            charges = dict(row.charges or {})
            if "current" in charges:
                charges["current"] = max(0, int(charges.get("current") or 0) - int(quantity))
                row.charges = charges
            else:
                row.quantity = max(0, int(row.quantity or 0) - int(quantity))
                if row.quantity == 0:
                    row.equipped_slot = None
            session.flush()
            after = self._item_dict(row)
            self._ledger(session, row.campaign_id, row.id, "use", before, after, actor, reason)
            return self._item_info(row)

    def delete_item(self, item_id: str, *, actor: str = "runtime", reason: str = "") -> dict[str, Any]:
        with self.database.transaction() as session:
            row = self._item(session, item_id)
            before = self._item_dict(row)
            campaign_id = row.campaign_id
            session.delete(row)
            self._ledger(session, campaign_id, item_id, "delete", before, None, actor, reason)
            return before

    def history(self, *, campaign_id: str, item_id: str | None = None) -> list[ItemLedgerInfo]:
        statement = select(ItemLedgerEntry).where(ItemLedgerEntry.campaign_id == campaign_id)
        if item_id:
            statement = statement.where(ItemLedgerEntry.item_id == item_id)
        statement = statement.order_by(ItemLedgerEntry.created_at, ItemLedgerEntry.id)
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            return [self._ledger_info(row) for row in session.scalars(statement)]

    def import_inventory(
        self,
        *,
        campaign_id: str,
        character_id: str,
        inventory: list[dict[str, Any]],
        replace: bool = False,
        actor: str = "runtime",
    ) -> list[ItemInfo]:
        with self.database.transaction() as session:
            self._campaign(session, campaign_id)
            character = session.get(Character, character_id)
            if character is None or character.campaign_id != campaign_id:
                raise LookupError(character_id)
            if replace:
                rows = list(
                    session.scalars(
                        select(ItemInstance).where(
                            ItemInstance.campaign_id == campaign_id,
                            ItemInstance.owner_type == "character",
                            ItemInstance.owner_id == character_id,
                        )
                    )
                )
                for row in rows:
                    before = self._item_dict(row)
                    session.delete(row)
                    self._ledger(
                        session,
                        campaign_id,
                        row.id,
                        "delete",
                        before,
                        None,
                        actor,
                        "replace character inventory",
                    )
            result: list[ItemInfo] = []
            for item in inventory:
                template = None
                template_data = dict(item.get("template") or {})
                source_key = template_data.get("source_key")
                if source_key:
                    template = session.scalar(
                        select(ItemTemplate).where(
                            ItemTemplate.system_id == character.system_id,
                            ItemTemplate.source_key == source_key,
                        )
                    )
                if template is None and (source_key or template_data):
                    template = ItemTemplate(
                        id=str(uuid.uuid4()),
                        system_id=character.system_id,
                        source_key=source_key or f"custom:{item['name'].lower().replace(' ', '-')}",
                        name=template_data.get("name") or item["name"],
                        category=item.get("category") or template_data.get("category") or "gear",
                        rarity=template_data.get("rarity") or "",
                        tags=list(template_data.get("tags") or []),
                        weight=int(template_data.get("weight") or 0),
                        value=dict(template_data.get("value") or {}),
                        rules=dict(template_data.get("rules") or {}),
                        description=template_data.get("description") or "",
                        metadata_json=dict(template_data.get("metadata") or {}),
                    )
                    session.add(template)
                    session.flush()
                row = ItemInstance(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    template_id=template.id if template else None,
                    name=item["name"],
                    owner_type="character",
                    owner_id=character_id,
                    quantity=item["quantity"],
                    equipped_slot=item.get("equipped_slot"),
                    attunement=item.get("attunement", "none"),
                    identified=bool(item.get("identified", True)),
                    charges=dict(item.get("charges") or {}),
                    condition=item.get("condition", "normal"),
                    state=dict(item.get("state") or {}),
                )
                session.add(row)
                session.flush()
                after = self._item_dict(row)
                self._ledger(session, campaign_id, row.id, "add", None, after, actor, "import inventory")
                result.append(self._item_info(row))
            return result

    def character_inventory(self, character_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            character = session.get(Character, character_id)
            if character is None:
                raise LookupError(character_id)
            if character.campaign_id is None:
                return []
            rows = session.scalars(
                select(ItemInstance)
                .where(
                    ItemInstance.campaign_id == character.campaign_id,
                    ItemInstance.owner_type == "character",
                    ItemInstance.owner_id == character_id,
                )
                .order_by(ItemInstance.name, ItemInstance.id)
            )
            return [self._item_dict(row) for row in rows]

    @staticmethod
    def _validate_owner(owner_type: str, owner_id: str) -> None:
        if owner_type not in OWNER_TYPES:
            raise ValueError(f"owner_type must be one of {', '.join(sorted(OWNER_TYPES))}")
        if not owner_id:
            raise ValueError("owner_id is required")

    @staticmethod
    def _campaign(session, campaign_id: str) -> Campaign:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    @staticmethod
    def _item(session, item_id: str) -> ItemInstance:
        row = session.get(ItemInstance, item_id)
        if row is None:
            raise LookupError(item_id)
        return row

    @staticmethod
    def _ledger(
        session,
        campaign_id: str,
        item_id: str | None,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        actor: str,
        reason: str,
    ) -> None:
        if operation == "delete":
            item_id = None
        session.add(
            ItemLedgerEntry(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                item_id=item_id,
                operation=operation,
                actor=actor,
                reason=reason,
                before=before,
                after=after,
            )
        )

    @staticmethod
    def _template_info(row: ItemTemplate) -> ItemTemplateInfo:
        return ItemTemplateInfo(
            id=row.id,
            system_id=row.system_id,
            source_key=row.source_key,
            name=row.name,
            category=row.category,
            rarity=row.rarity,
            tags=list(row.tags or []),
            weight=row.weight,
            value=dict(row.value or {}),
            rules=dict(row.rules or {}),
            description=row.description,
            metadata=dict(row.metadata_json or {}),
        )

    @staticmethod
    def _item_dict(row: ItemInstance) -> dict[str, Any]:
        return {
            "id": row.id,
            "campaign_id": row.campaign_id,
            "template_id": row.template_id,
            "name": row.name,
            "owner_type": row.owner_type,
            "owner_id": row.owner_id,
            "container_id": row.container_id,
            "quantity": row.quantity,
            "equipped_slot": row.equipped_slot,
            "attunement": row.attunement,
            "identified": row.identified,
            "charges": dict(row.charges or {}),
            "condition": row.condition,
            "state": dict(row.state or {}),
        }

    @classmethod
    def _item_info(cls, row: ItemInstance) -> ItemInfo:
        return ItemInfo(**cls._item_dict(row))

    @staticmethod
    def _ledger_info(row: ItemLedgerEntry) -> ItemLedgerInfo:
        return ItemLedgerInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            item_id=row.item_id,
            operation=row.operation,
            actor=row.actor,
            reason=row.reason,
            before=dict(row.before) if row.before else None,
            after=dict(row.after) if row.after else None,
            created_at=row.created_at.isoformat(),
        )
