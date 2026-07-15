"""Validated, content-addressed rule packs and branch-local campaign locks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, func, select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.database import Database
from sagasmith_core.models import (
    Campaign,
    CampaignRuleActivation,
    CampaignRuleProfile,
    CampaignSnapshot,
    RulePack,
    RulePackVersion,
)

PACK_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class RulePackError(ValueError):
    pass


class RulesetUnavailableError(RulePackError):
    pass


@dataclass(frozen=True)
class RulePackVersionInfo:
    pack_id: str
    version: str
    checksum: str
    status: str
    manifest: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]
    mechanics: tuple[dict[str, Any], ...]
    validation_report: dict[str, Any]


@dataclass(frozen=True)
class RuleActivationInfo:
    campaign_id: str
    branch_id: str
    pack_id: str
    version: str
    checksum: str
    enabled: bool
    options: dict[str, Any]


@dataclass(frozen=True)
class EffectiveRulesetInfo:
    campaign_id: str
    branch_id: str
    system_id: str
    edition: str
    fingerprint: str
    lock: tuple[dict[str, Any], ...]
    mechanics: tuple[dict[str, Any], ...]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RulePackService:
    """Owns pack lifecycle; install and campaign activation are deliberately separate."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def save_draft(
        self,
        *,
        manifest: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
        mechanics: list[dict[str, Any]] | None = None,
        provenance: dict[str, Any] | None = None,
        additional_errors: list[str] | None = None,
    ) -> RulePackVersionInfo:
        manifest = dict(manifest)
        artifacts = [dict(item) for item in artifacts or []]
        mechanics = [dict(item) for item in mechanics or []]
        report = self.validate_definition(manifest, artifacts, mechanics)
        if additional_errors:
            report["errors"] = [*report["errors"], *additional_errors]
            report["valid"] = False
        pack_id = str(manifest.get("id") or "")
        version = str(manifest.get("version") or "")
        checksum = content_checksum(
            {"manifest": manifest, "artifacts": artifacts, "mechanics": mechanics}
        )
        if (
            not PACK_ID_RE.fullmatch(pack_id)
            or not VERSION_RE.fullmatch(version)
            or not str(manifest.get("system_id") or "")
        ):
            return RulePackVersionInfo(
                pack_id=pack_id,
                version=version,
                checksum=checksum,
                status="rejected",
                manifest=manifest,
                artifacts=tuple(artifacts),
                mechanics=tuple(mechanics),
                validation_report=report,
            )
        with self.database.transaction() as session:
            pack = session.get(RulePack, pack_id)
            if pack is None:
                pack = RulePack(
                    id=pack_id,
                    system_id=str(manifest["system_id"]),
                    title=str(manifest.get("title") or pack_id),
                    namespace=str(manifest.get("namespace") or pack_id),
                    provenance=dict(provenance or manifest.get("provenance") or {}),
                )
                session.add(pack)
            elif pack.system_id != manifest["system_id"]:
                raise RulePackError("a rule pack cannot change system_id between versions")
            row = session.get(RulePackVersion, {"pack_id": pack_id, "version": version})
            if row is not None and row.status == "installed" and row.checksum != checksum:
                raise RulePackError("installed rule-pack versions are immutable")
            if row is not None and row.status == "installed":
                # Installed content and its validation evidence are immutable.
                # Re-submitting the same checksum is an idempotent read.
                return self._version_info(row)
            was_installed = row is not None and row.status == "installed"
            if row is None:
                row = RulePackVersion(pack_id=pack_id, version=version)
                session.add(row)
            row.manifest = manifest
            row.artifacts = artifacts
            row.mechanics = mechanics
            row.checksum = checksum
            row.status = (
                "installed" if was_installed else ("validated" if report["valid"] else "rejected")
            )
            row.validation_report = report
            session.flush()
            return self._version_info(row)

    def install(self, pack_id: str, version: str) -> RulePackVersionInfo:
        with self.database.transaction() as session:
            row = session.get(RulePackVersion, {"pack_id": pack_id, "version": version})
            if row is None:
                raise LookupError(f"{pack_id}@{version}")
            if row.status not in {"validated", "installed"}:
                raise RulePackError("only a validated rule-pack version can be installed")
            row.status = "installed"
            session.flush()
            return self._version_info(row)

    def list_versions(self, pack_id: str | None = None) -> list[RulePackVersionInfo]:
        with self.database.transaction() as session:
            query = select(RulePackVersion).order_by(
                RulePackVersion.pack_id, RulePackVersion.version
            )
            if pack_id:
                query = query.where(RulePackVersion.pack_id == pack_id)
            return [self._version_info(row) for row in session.scalars(query)]

    def get_version(self, pack_id: str, version: str) -> RulePackVersionInfo:
        with self.database.transaction() as session:
            row = session.get(RulePackVersion, {"pack_id": pack_id, "version": version})
            if row is None:
                raise LookupError(f"{pack_id}@{version}")
            return self._version_info(row)

    def remove_version(self, pack_id: str, version: str) -> None:
        """Remove an unreferenced version; historical branch locks are never broken."""
        with self.database.transaction() as session:
            row = session.get(RulePackVersion, {"pack_id": pack_id, "version": version})
            if row is None:
                raise LookupError(f"{pack_id}@{version}")
            references = session.scalar(
                select(func.count())
                .select_from(CampaignRuleActivation)
                .where(
                    CampaignRuleActivation.pack_id == pack_id,
                    CampaignRuleActivation.version == version,
                )
            )
            if references:
                raise RulePackError(
                    "a rule-pack version referenced by a branch lock cannot be removed"
                )
            historical_reference = any(
                item.get("pack_id") == pack_id and item.get("version") == version
                for snapshot in session.scalars(select(CampaignSnapshot))
                for item in dict(snapshot.payload or {}).get("rule_lock", [])
            )
            if historical_reference:
                raise RulePackError(
                    "a rule-pack version referenced by a snapshot cannot be removed"
                )
            session.delete(row)
            session.flush()
            remaining = session.scalar(
                select(func.count())
                .select_from(RulePackVersion)
                .where(RulePackVersion.pack_id == pack_id)
            )
            if not remaining:
                pack = session.get(RulePack, pack_id)
                if pack is not None:
                    session.delete(pack)

    def set_activation(
        self,
        campaign_id: str,
        *,
        pack_id: str,
        version: str,
        enabled: bool = True,
        options: dict[str, Any] | None = None,
        branch_id: str | None = None,
        expected_campaign_revision: int | None = None,
    ) -> RuleActivationInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            branch = resolve_branch(session, campaign, branch_id)
            if dict(campaign.state or {}).get("combat", {}).get("active", False):
                raise RulePackError("rule-pack activation cannot change during active combat")
            version_row = session.get(RulePackVersion, {"pack_id": pack_id, "version": version})
            if version_row is None or version_row.status != "installed":
                raise RulePackError("the exact rule-pack version must be installed first")
            pack = session.get(RulePack, pack_id)
            if pack is None or pack.system_id != campaign.system_id:
                raise RulePackError("rule pack is incompatible with the campaign system")
            profile = session.get(CampaignRuleProfile, campaign_id)
            edition = profile.edition if profile else ""
            supported = [str(item) for item in version_row.manifest.get("editions", [])]
            if supported and edition and edition not in supported:
                raise RulePackError(f"rule pack does not support campaign edition {edition}")
            row = session.get(
                CampaignRuleActivation,
                {"campaign_id": campaign_id, "branch_id": branch.id, "pack_id": pack_id},
            )
            if row is None:
                row = CampaignRuleActivation(
                    campaign_id=campaign_id, branch_id=branch.id, pack_id=pack_id
                )
                session.add(row)
            row.version = version
            row.checksum = version_row.checksum
            row.enabled = bool(enabled)
            row.options = dict(options or {})
            campaign.revision += 1
            session.flush()
            self._resolve(session, campaign, branch.id)
            return self._activation_info(row)

    def remove_activation(
        self,
        campaign_id: str,
        pack_id: str,
        *,
        branch_id: str | None = None,
        expected_campaign_revision: int | None = None,
    ) -> None:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if (
                expected_campaign_revision is not None
                and campaign.revision != expected_campaign_revision
            ):
                raise ValueError(
                    "campaign revision conflict: "
                    f"expected {expected_campaign_revision}, found {campaign.revision}"
                )
            if dict(campaign.state or {}).get("combat", {}).get("active", False):
                raise RulePackError("rule-pack activation cannot change during active combat")
            branch = resolve_branch(session, campaign, branch_id)
            result = session.execute(
                delete(CampaignRuleActivation).where(
                    CampaignRuleActivation.campaign_id == campaign_id,
                    CampaignRuleActivation.branch_id == branch.id,
                    CampaignRuleActivation.pack_id == pack_id,
                )
            )
            if not result.rowcount:
                raise LookupError(f"rule-pack activation not found: {pack_id}")
            campaign.revision += 1

    def activations(
        self, campaign_id: str, *, branch_id: str | None = None
    ) -> list[RuleActivationInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            rows = session.scalars(
                select(CampaignRuleActivation)
                .where(
                    CampaignRuleActivation.campaign_id == campaign_id,
                    CampaignRuleActivation.branch_id == branch.id,
                )
                .order_by(CampaignRuleActivation.pack_id)
            )
            return [self._activation_info(row) for row in rows]

    def effective_ruleset(
        self, campaign_id: str, *, branch_id: str | None = None
    ) -> EffectiveRulesetInfo:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            return self._resolve(session, campaign, branch.id)

    def assert_edition_compatible(
        self,
        campaign_id: str,
        edition: str,
        *,
        branch_id: str | None = None,
    ) -> None:
        """Reject a profile change that would invalidate an enabled branch lock."""
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            self._assert_edition_compatible_in_session(
                session,
                campaign,
                edition,
                branch_id=branch_id,
            )

    @staticmethod
    def _assert_edition_compatible_in_session(
        session,
        campaign: Campaign,
        edition: str,
        *,
        branch_id: str | None = None,
    ) -> None:
        query = select(CampaignRuleActivation).where(
            CampaignRuleActivation.campaign_id == campaign.id,
            CampaignRuleActivation.enabled.is_(True),
        )
        if branch_id is not None:
            branch = resolve_branch(session, campaign, branch_id)
            query = query.where(CampaignRuleActivation.branch_id == branch.id)
        for activation in session.scalars(query):
            version = session.get(
                RulePackVersion,
                {"pack_id": activation.pack_id, "version": activation.version},
            )
            pack = session.get(RulePack, activation.pack_id)
            if (
                version is None
                or version.status != "installed"
                or version.checksum != activation.checksum
                or pack is None
                or pack.system_id != campaign.system_id
            ):
                raise RulesetUnavailableError(
                    f"locked rule pack unavailable: {activation.pack_id}@{activation.version}"
                )
            supported = [str(item) for item in version.manifest.get("editions", [])]
            if supported and edition not in supported:
                raise RulePackError(
                    f"{activation.pack_id}@{activation.version} "
                    f"does not support campaign edition {edition}"
                )

    @staticmethod
    def validate_definition(
        manifest: dict[str, Any],
        artifacts: list[dict[str, Any]],
        mechanics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        errors: list[str] = []
        pack_id = str(manifest.get("id") or "")
        version = str(manifest.get("version") or "")
        if not PACK_ID_RE.fullmatch(pack_id):
            errors.append("manifest.id must be a namespaced lowercase identifier")
        if not VERSION_RE.fullmatch(version):
            errors.append("manifest.version must be semantic version x.y.z")
        if not str(manifest.get("system_id") or ""):
            errors.append("manifest.system_id is required")
        namespace = str(manifest.get("namespace") or pack_id)
        if namespace != pack_id:
            errors.append("manifest.namespace must equal manifest.id")
        if not isinstance(manifest.get("editions", []), list):
            errors.append("manifest.editions must be a list")
        for field in ("dependencies", "conflicts", "capabilities"):
            if field in manifest and not isinstance(manifest[field], list):
                errors.append(f"manifest.{field} must be a list")
        ids: set[str] = set()
        declared_capabilities = {str(item) for item in manifest.get("capabilities", [])}
        for index, mechanic in enumerate(mechanics):
            mechanic_id = str(mechanic.get("id") or "")
            if not mechanic_id.startswith(f"{pack_id}."):
                errors.append(f"mechanics[{index}].id must use the pack namespace")
            if mechanic_id in ids:
                errors.append(f"duplicate mechanic id: {mechanic_id}")
            ids.add(mechanic_id)
            if not str(mechanic.get("event") or ""):
                errors.append(f"mechanics[{index}].event is required")
            elif str(mechanic["event"]) not in declared_capabilities:
                errors.append(f"mechanics[{index}].event is not declared in manifest.capabilities")
            if not isinstance(mechanic.get("operations", []), list):
                errors.append(f"mechanics[{index}].operations must be a list")
        artifact_ids = [str(item.get("id") or "") for item in artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            errors.append("artifact ids must be unique")
        if any(not item.startswith(f"{pack_id}.") for item in artifact_ids):
            errors.append("artifact ids must use the pack namespace")
        for index, artifact in enumerate(artifacts):
            if not str(artifact.get("kind") or "").strip():
                errors.append(f"artifacts[{index}].kind is required")
            card = artifact.get("card")
            if not isinstance(card, dict):
                errors.append(f"artifacts[{index}].card must be an object")
            elif not str(card.get("name") or "").strip():
                errors.append(f"artifacts[{index}].card.name is required")
            for field in ("rule_refs", "source_citations"):
                if field in artifact and not isinstance(artifact[field], list):
                    errors.append(f"artifacts[{index}].{field} must be a list")
            refs = artifact.get("mechanic_refs", [])
            if not isinstance(refs, list):
                errors.append(f"artifacts[{index}].mechanic_refs must be a list")
                refs = []
            unknown_refs = sorted({str(item) for item in refs if str(item) not in ids})
            if unknown_refs:
                errors.append(
                    f"artifacts[{index}].mechanic_refs are unknown: {', '.join(unknown_refs)}"
                )
        return {"valid": not errors, "errors": errors}

    @staticmethod
    def _resolve(session, campaign: Campaign, branch_id: str) -> EffectiveRulesetInfo:
        profile = session.get(CampaignRuleProfile, campaign.id)
        rows = list(
            session.scalars(
                select(CampaignRuleActivation)
                .where(
                    CampaignRuleActivation.campaign_id == campaign.id,
                    CampaignRuleActivation.branch_id == branch_id,
                    CampaignRuleActivation.enabled.is_(True),
                )
                .order_by(CampaignRuleActivation.pack_id)
            )
        )
        versions: dict[str, RulePackVersion] = {}
        for activation in rows:
            version = session.get(
                RulePackVersion,
                {"pack_id": activation.pack_id, "version": activation.version},
            )
            if (
                version is None
                or version.status != "installed"
                or version.checksum != activation.checksum
            ):
                raise RulesetUnavailableError(
                    f"locked rule pack unavailable: {activation.pack_id}@{activation.version}"
                )
            pack = session.get(RulePack, activation.pack_id)
            if pack is None or pack.system_id != campaign.system_id:
                raise RulesetUnavailableError(
                    f"locked rule pack is incompatible with campaign system: "
                    f"{activation.pack_id}@{activation.version}"
                )
            versions[activation.pack_id] = version
        enabled_ids = set(versions)
        for pack_id, version in versions.items():
            supported = [str(item) for item in version.manifest.get("editions", [])]
            if supported and profile and profile.edition not in supported:
                raise RulePackError(
                    f"{pack_id}@{version.version} does not support "
                    f"campaign edition {profile.edition}"
                )
            dependency_items = list(version.manifest.get("dependencies", []))
            dependencies = {
                str(item.get("id") if isinstance(item, dict) else item) for item in dependency_items
            }
            missing = sorted(dependencies - enabled_ids)
            if missing:
                raise RulePackError(f"{pack_id} has missing dependencies: {', '.join(missing)}")
            for item in dependency_items:
                if not isinstance(item, dict) or not item.get("version"):
                    continue
                dependency_id = str(item.get("id") or "")
                dependency = versions.get(dependency_id)
                if dependency is not None and dependency.version != str(item["version"]):
                    raise RulePackError(f"{pack_id} requires {dependency_id}@{item['version']}")
            conflicts = {
                str(item.get("id") if isinstance(item, dict) else item)
                for item in version.manifest.get("conflicts", [])
            }
            active_conflicts = sorted(conflicts & enabled_ids)
            if active_conflicts:
                raise RulePackError(f"{pack_id} conflicts with: {', '.join(active_conflicts)}")
        mechanics: list[dict[str, Any]] = []
        mechanic_ids: set[str] = set()
        patch_targets: set[str] = set()
        available_mechanics = {
            str(mechanic["id"]): dict(mechanic)
            for version in versions.values()
            for mechanic in version.mechanics
        }
        for pack_id in sorted(versions):
            for mechanic in versions[pack_id].mechanics:
                mechanic_id = str(mechanic["id"])
                if mechanic_id in mechanic_ids:
                    raise RulePackError(f"duplicate active mechanic id: {mechanic_id}")
                mechanic_ids.add(mechanic_id)
                patch = mechanic.get("patch")
                if patch:
                    target = str(patch.get("target") or "")
                    expected = str(patch.get("expected_checksum") or "")
                    if not target or not expected:
                        raise RulePackError("patches need target and expected_checksum")
                    if target in patch_targets:
                        raise RulePackError(f"multiple rule packs patch {target}")
                    target_mechanic = available_mechanics.get(target)
                    if target_mechanic is None:
                        raise RulePackError(
                            f"patch target is unavailable or requires a native provider: {target}"
                        )
                    if content_checksum(target_mechanic) != expected:
                        raise RulePackError(f"patch checksum mismatch for {target}")
                    patch_targets.add(target)
                mechanics.append(dict(mechanic))
        if patch_targets:
            mechanics = [
                mechanic for mechanic in mechanics if str(mechanic["id"]) not in patch_targets
            ]
        lock = tuple(
            {
                "pack_id": row.pack_id,
                "version": row.version,
                "checksum": row.checksum,
                "options": dict(row.options),
            }
            for row in rows
        )
        base = {
            "system_id": campaign.system_id,
            "edition": profile.edition if profile else "",
            "lock": lock,
        }
        return EffectiveRulesetInfo(
            campaign_id=campaign.id,
            branch_id=branch_id,
            system_id=campaign.system_id,
            edition=profile.edition if profile else "",
            fingerprint=content_checksum(base),
            lock=lock,
            mechanics=tuple(mechanics),
        )

    @staticmethod
    def _version_info(row: RulePackVersion) -> RulePackVersionInfo:
        return RulePackVersionInfo(
            pack_id=row.pack_id,
            version=row.version,
            checksum=row.checksum,
            status=row.status,
            manifest=dict(row.manifest),
            artifacts=tuple(dict(item) for item in row.artifacts),
            mechanics=tuple(dict(item) for item in row.mechanics),
            validation_report=dict(row.validation_report),
        )

    @staticmethod
    def _activation_info(row: CampaignRuleActivation) -> RuleActivationInfo:
        return RuleActivationInfo(
            campaign_id=row.campaign_id,
            branch_id=row.branch_id,
            pack_id=row.pack_id,
            version=row.version,
            checksum=row.checksum,
            enabled=row.enabled,
            options=dict(row.options),
        )


def serialize_effective_ruleset(value: EffectiveRulesetInfo) -> dict[str, Any]:
    return asdict(value)
