"""Portable addon library and branch-local activation lifecycle."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from sagasmith_core.branches import resolve_branch
from sagasmith_core.campaigns import CampaignNotFoundError
from sagasmith_core.concurrency import compare_and_swap_campaign
from sagasmith_core.content_pack import validate_content_package
from sagasmith_core.database import Database
from sagasmith_core.models import (
    Campaign,
    CampaignAddonActivation,
    CampaignRuleActivation,
    CampaignRuleProfile,
    CampaignSnapshot,
    ContentAddon,
    ContentAddonVersion,
    RulePack,
    RulePackVersion,
)
from sagasmith_core.rule_packs import RulePackService
from sagasmith_core.runtime_locks import require_mutation_unlocked


class AddonError(ValueError):
    """Raised when an addon lifecycle transition would break an exact lock."""


_MAX_ADDON_RULE_DEPENDENCY_DEPTH = 128
_MAX_ADDON_RULE_DEPENDENCY_PACKS = 1024


@dataclass(frozen=True)
class AddonVersionInfo:
    addon_id: str
    version: str
    checksum: str
    status: str
    system_id: str
    title: str
    manifest: dict[str, Any]
    components: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]
    validation_report: dict[str, Any]


@dataclass(frozen=True)
class AddonActivationInfo:
    campaign_id: str
    branch_id: str
    addon_id: str
    version: str
    checksum: str
    enabled: bool
    component_locks: tuple[dict[str, Any], ...]
    options: dict[str, Any]


class AddonService:
    """Own addon import/install/activation without bypassing component services."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def import_package(
        self,
        package: dict[str, Any],
        *,
        provenance: dict[str, Any] | None = None,
    ) -> AddonVersionInfo:
        """Register an inspected addon; components remain inactive."""

        value = validate_content_package(package)
        if value["kind"] != "addon":
            raise AddonError("content package kind must be addon")
        addon_id = value["id"]
        version = value["version"]
        manifest = dict(value["manifest"])
        components = [
            {
                "kind": "rule_pack",
                "id": item["id"],
                "version": item["version"],
                "checksum": item["definition_checksum"],
                "optional": False,
            }
            for item in value["content"].get("rule_definitions") or []
        ]
        component_counts = Counter(item["kind"] for item in components)
        embedded_content = Counter(
            str(artifact.get("kind") or "unknown")
            for artifact in value["content"].get("artifacts") or []
        )
        if value["actors"]:
            embedded_content["actor_card"] += len(value["actors"])
        embedded_content.update(str(actor["actor_type"]) for actor in value["actors"])
        report = {
            "valid": True,
            "component_count": len(components),
            "component_counts": dict(sorted(component_counts.items())),
            "content_summary": dict(manifest.get("content_summary") or {}),
            "declared_content_summary": dict(manifest.get("content_summary") or {}),
            "embedded_content_summary": dict(sorted(embedded_content.items())),
        }
        imported_provenance = {
            "distribution": value["metadata"].get("distribution"),
            "license": value["metadata"].get("license"),
            "attribution": value["metadata"].get("attribution"),
            "content_package_checksum": value["checksum"],
            **dict(provenance or {}),
        }
        with self.database.transaction() as session:
            addon = session.get(ContentAddon, addon_id)
            if addon is None:
                addon = ContentAddon(
                    id=addon_id,
                    system_id=value["system_id"],
                    title=str(manifest["title"]),
                )
                session.add(addon)
            elif addon.system_id != value["system_id"]:
                raise AddonError("an addon cannot change system_id between versions")
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if row is not None and row.checksum != value["checksum"]:
                raise AddonError("addon versions are immutable once imported")
            if row is None:
                row = ContentAddonVersion(addon_id=addon_id, version=version)
                session.add(row)
            row.manifest = manifest
            row.components = components
            row.package = value
            row.provenance = imported_provenance
            row.checksum = value["checksum"]
            row.status = row.status if row.status == "installed" else "imported"
            row.validation_report = report
            session.flush()
            return self._version_info(row, addon)

    def install(self, addon_id: str, version: str) -> AddonVersionInfo:
        """Install only after every global rule/preset component is available."""

        with self.database.transaction() as session:
            addon = session.get(ContentAddon, addon_id)
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if addon is None or row is None:
                raise LookupError(f"{addon_id}@{version}")
            missing = self._global_component_errors(session, row)
            if missing:
                raise AddonError("addon components are not installed: " + "; ".join(missing))
            row.status = "installed"
            session.flush()
            return self._version_info(row, addon)

    def list_versions(self, addon_id: str | None = None) -> list[AddonVersionInfo]:
        with self.database.transaction() as session:
            statement = (
                select(ContentAddonVersion, ContentAddon)
                .join(ContentAddon, ContentAddon.id == ContentAddonVersion.addon_id)
                .order_by(ContentAddonVersion.addon_id, ContentAddonVersion.version)
            )
            if addon_id:
                statement = statement.where(ContentAddonVersion.addon_id == addon_id)
            return [self._version_info(row[0], row[1]) for row in session.execute(statement)]

    def get_version(self, addon_id: str, version: str) -> AddonVersionInfo:
        with self.database.transaction() as session:
            addon = session.get(ContentAddon, addon_id)
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if addon is None or row is None:
                raise LookupError(f"{addon_id}@{version}")
            return self._version_info(row, addon)

    def get_package(self, addon_id: str, version: str) -> dict[str, Any]:
        """Return the exact immutable content package stored at import time."""

        with self.database.transaction() as session:
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if row is None:
                raise LookupError(f"{addon_id}@{version}")
            value = validate_content_package(dict(row.package or {}))
            if value["kind"] != "addon":
                raise AddonError("stored content package kind is not addon")
            if value["checksum"] != row.checksum:
                raise AddonError("stored addon package does not match its immutable lock")
            return value

    def component_status(self, addon_id: str, version: str) -> list[dict[str, Any]]:
        """Report exact global component availability without changing state."""

        with self.database.transaction() as session:
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if row is None:
                raise LookupError(f"{addon_id}@{version}")
            verifications = list(
                dict(row.provenance or {}).get("component_equivalence_verifications") or []
            )
            return [
                self._component_status(
                    session,
                    component,
                    equivalence_verifications=verifications,
                )
                for component in row.components
            ]

    def record_component_equivalence(
        self,
        addon_id: str,
        version: str,
        *,
        kind: str,
        component_id: str,
        component_version: str,
        checksum: str,
        basis: str,
        proof_checksum: str,
    ) -> dict[str, Any]:
        """Record plugin-proven equivalence with an installed local component.

        A system plugin can rebuild a content definition from an already
        installed local pack even when that pack was not originally imported
        from the addon envelope.  Core does not interpret system-specific
        definitions; it stores the exact addon-scoped proof after checking the
        component lock and local identity.
        """

        normalized_basis = str(basis).strip()
        normalized_proof = str(proof_checksum).strip().lower()
        if not normalized_basis or len(normalized_basis) > 100:
            raise AddonError("component equivalence basis must contain 1 to 100 characters")
        if len(normalized_proof) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_proof
        ):
            raise AddonError("component equivalence proof_checksum must be sha256")
        identity = (str(kind), str(component_id), str(component_version))
        with self.database.transaction() as session:
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if row is None:
                raise LookupError(f"{addon_id}@{version}")
            matches = [
                dict(component)
                for component in row.components
                if (
                    str(component.get("kind") or ""),
                    str(component.get("id") or ""),
                    str(component.get("version") or ""),
                )
                == identity
            ]
            if len(matches) != 1 or str(matches[0].get("checksum") or "") != checksum:
                raise AddonError("component equivalence does not match one exact addon lock")
            local = session.get(
                RulePackVersion,
                {"pack_id": component_id, "version": component_version},
            )
            if local is None:
                raise AddonError("component equivalence requires the exact local component")
            verification = {
                "kind": identity[0],
                "id": identity[1],
                "version": identity[2],
                "checksum": str(checksum),
                "basis": normalized_basis,
                "proof_checksum": normalized_proof,
            }
            provenance = dict(row.provenance or {})
            existing = [
                dict(item)
                for item in provenance.get("component_equivalence_verifications") or []
                if isinstance(item, dict)
                and (
                    str(item.get("kind") or ""),
                    str(item.get("id") or ""),
                    str(item.get("version") or ""),
                )
                != identity
            ]
            provenance["component_equivalence_verifications"] = [
                *existing,
                verification,
            ]
            row.provenance = provenance
            session.flush()
            return verification

    def activation_requirements(
        self,
        campaign_id: str,
        *,
        addon_id: str,
        version: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Preflight an enable operation before campaign module materialization."""

        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            require_mutation_unlocked(
                campaign.state,
                "addon_activation",
                error_type=AddonError,
            )
            branch = resolve_branch(session, campaign, branch_id)
            addon = session.get(ContentAddon, addon_id)
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if addon is None or row is None or row.status != "installed":
                raise AddonError("the exact addon version must be installed first")
            if addon.system_id != campaign.system_id:
                raise AddonError("addon is incompatible with the campaign system")
            profile = session.get(CampaignRuleProfile, campaign_id)
            editions = {str(item) for item in row.manifest.get("editions", [])}
            if profile is not None and editions and profile.edition not in editions:
                raise AddonError(f"addon does not support campaign edition {profile.edition}")
            global_errors = self._global_component_errors(session, row)
            if global_errors:
                raise AddonError("addon components are not installed: " + "; ".join(global_errors))
            self._rule_dependency_components(
                session,
                campaign=campaign,
                components=list(row.components or []),
            )
            self._assert_addon_conflicts(
                session,
                campaign_id=campaign_id,
                branch_id=branch.id,
                addon_id=addon_id,
                manifest=row.manifest,
                enabled=True,
            )
            return {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision,
                "branch_id": branch.id,
                "addon_id": addon_id,
                "version": version,
                "checksum": row.checksum,
                "manifest": dict(row.manifest or {}),
                "components": [dict(item) for item in row.components or []],
            }

    def set_activation(
        self,
        campaign_id: str,
        *,
        addon_id: str,
        version: str,
        enabled: bool = True,
        options: dict[str, Any] | None = None,
        branch_id: str | None = None,
        expected_campaign_revision: int | None = None,
    ) -> AddonActivationInfo:
        """Atomically lock an addon and all of its rule components to a branch."""

        addon_options = dict(options or {})
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            base_revision = (
                campaign.revision
                if expected_campaign_revision is None
                else expected_campaign_revision
            )
            require_mutation_unlocked(
                campaign.state,
                "addon_activation",
                error_type=AddonError,
            )
            branch = resolve_branch(session, campaign, branch_id)
            addon = session.get(ContentAddon, addon_id)
            version_row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if addon is None or version_row is None or version_row.status != "installed":
                raise AddonError("the exact addon version must be installed first")
            if addon.system_id != campaign.system_id:
                raise AddonError("addon is incompatible with the campaign system")
            profile = session.get(CampaignRuleProfile, campaign_id)
            editions = {str(item) for item in version_row.manifest.get("editions", [])}
            if profile is not None and editions and profile.edition not in editions:
                raise AddonError(f"addon does not support campaign edition {profile.edition}")
            self._assert_addon_conflicts(
                session,
                campaign_id=campaign_id,
                branch_id=branch.id,
                addon_id=addon_id,
                manifest=version_row.manifest,
                enabled=enabled,
            )
            row = session.get(
                CampaignAddonActivation,
                {
                    "campaign_id": campaign_id,
                    "branch_id": branch.id,
                    "addon_id": addon_id,
                },
            )
            if not enabled:
                if row is None or not row.enabled:
                    raise AddonError("the exact addon version is not active on this branch")
                if row.version != version or row.checksum != version_row.checksum:
                    raise AddonError(
                        "addon disable must match the active exact version: "
                        f"{row.version}@{row.checksum}"
                    )
            previous_version = None
            previous_options: dict[str, Any] = {}
            if (
                enabled
                and row is not None
                and row.enabled
                and (row.version != version or row.checksum != version_row.checksum)
            ):
                previous_version = session.get(
                    ContentAddonVersion,
                    {"addon_id": addon_id, "version": row.version},
                )
                if previous_version is None or previous_version.checksum != row.checksum:
                    raise AddonError("the active addon version is unavailable for replacement")
                previous_options = dict(row.options or {})
            dependency_components = self._rule_dependency_components(
                session,
                campaign=campaign,
                components=list(version_row.components or []),
            )
            previous_dependency_components = (
                self._rule_dependency_components(
                    session,
                    campaign=campaign,
                    components=list(previous_version.components or []),
                )
                if previous_version is not None
                else []
            )
            if row is None:
                row = CampaignAddonActivation(
                    campaign_id=campaign_id,
                    branch_id=branch.id,
                    addon_id=addon_id,
                )
                session.add(row)
            if previous_version is not None:
                self._set_rule_component_ownership(
                    session,
                    campaign=campaign,
                    branch_id=branch.id,
                    addon_id=addon_id,
                    components=list(previous_version.components or []),
                    enabled=False,
                    addon_options=previous_options,
                )
                self._set_rule_component_ownership(
                    session,
                    campaign=campaign,
                    branch_id=branch.id,
                    addon_id=addon_id,
                    components=previous_dependency_components,
                    enabled=False,
                    addon_options={},
                )
            row.version = version
            row.checksum = version_row.checksum
            row.enabled = bool(enabled)
            row.component_locks = [dict(item) for item in version_row.components]
            row.options = addon_options
            self._set_rule_component_ownership(
                session,
                campaign=campaign,
                branch_id=branch.id,
                addon_id=addon_id,
                components=list(version_row.components),
                enabled=enabled,
                addon_options=addon_options,
            )
            self._set_rule_component_ownership(
                session,
                campaign=campaign,
                branch_id=branch.id,
                addon_id=addon_id,
                components=dependency_components,
                enabled=enabled,
                addon_options={},
            )
            compare_and_swap_campaign(
                session,
                campaign_id,
                expected_revision=base_revision,
            )
            session.expire(campaign)
            session.refresh(campaign)
            session.flush()
            RulePackService._resolve(session, campaign, branch.id)
            return self._activation_info(row)

    def activations(
        self, campaign_id: str, *, branch_id: str | None = None
    ) -> list[AddonActivationInfo]:
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            branch = resolve_branch(session, campaign, branch_id)
            rows = session.scalars(
                select(CampaignAddonActivation)
                .where(
                    CampaignAddonActivation.campaign_id == campaign_id,
                    CampaignAddonActivation.branch_id == branch.id,
                )
                .order_by(CampaignAddonActivation.addon_id)
            )
            return [self._activation_info(row) for row in rows]

    def remove_version(self, addon_id: str, version: str) -> None:
        with self.database.transaction() as session:
            row = session.get(
                ContentAddonVersion,
                {"addon_id": addon_id, "version": version},
            )
            if row is None:
                raise LookupError(f"{addon_id}@{version}")
            references = session.scalar(
                select(func.count())
                .select_from(CampaignAddonActivation)
                .where(
                    CampaignAddonActivation.addon_id == addon_id,
                    CampaignAddonActivation.version == version,
                )
            )
            if references:
                raise AddonError("an activated addon version cannot be removed")
            from sagasmith_core.snapshots import SnapshotService

            historical_reference = any(
                item.get("addon_id") == addon_id and item.get("version") == version
                for snapshot in session.scalars(select(CampaignSnapshot))
                for item in SnapshotService._materialize(snapshot).get("addon_lock", [])
            )
            if historical_reference:
                raise AddonError("an addon version referenced by a snapshot cannot be removed")
            session.delete(row)
            remaining = session.scalar(
                select(func.count())
                .select_from(ContentAddonVersion)
                .where(ContentAddonVersion.addon_id == addon_id)
            )
            if not remaining:
                addon = session.get(ContentAddon, addon_id)
                if addon is not None:
                    session.delete(addon)

    @staticmethod
    def _component_status(
        session,
        component: dict[str, Any],
        *,
        equivalence_verifications: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        kind = str(component["kind"])
        row = session.get(
            RulePackVersion,
            {"pack_id": component["id"], "version": component["version"]},
        )
        if row is None:
            return {**dict(component), "status": "missing"}
        provenance = dict(row.provenance or {})
        actual_checksum = str(
            dict(provenance.get("content_definition") or {}).get("definition_checksum") or ""
        )
        equivalence_verified = any(
            str(item.get("kind") or "") == kind
            and str(item.get("id") or "") == str(component["id"])
            and str(item.get("version") or "") == str(component["version"])
            and str(item.get("checksum") or "") == str(component["checksum"])
            for item in equivalence_verifications or []
            if isinstance(item, dict)
        )
        checksum_status = (
            "match"
            if actual_checksum == component["checksum"] or equivalence_verified
            else "unverified"
            if not actual_checksum
            else "conflict"
        )
        return {
            **dict(component),
            "status": row.status,
            "checksum_status": checksum_status,
        }

    @classmethod
    def _global_component_errors(cls, session, row: ContentAddonVersion) -> list[str]:
        errors = []
        verifications = list(
            dict(row.provenance or {}).get("component_equivalence_verifications") or []
        )
        for component in row.components:
            status = cls._component_status(
                session,
                dict(component),
                equivalence_verifications=verifications,
            )
            if status["status"] != "installed":
                errors.append(
                    f"{component['kind']}:{component['id']}@{component['version']} "
                    f"is {status['status']}"
                )
            elif status["checksum_status"] != "match":
                errors.append(
                    f"{component['kind']}:{component['id']}@{component['version']} "
                    f"checksum is {status['checksum_status']}"
                )
        return errors

    @staticmethod
    def _rule_dependency_components(
        session,
        *,
        campaign: Campaign,
        components: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve the exact installed dependency closure for addon rule components."""

        profile = session.get(CampaignRuleProfile, campaign.id)
        edition = profile.edition if profile is not None else ""
        root_ids = {
            str(component["id"])
            for component in components
            if str(component.get("kind") or "") == "rule_pack"
        }
        selected: dict[str, tuple[str, str]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        visited: set[str] = set()

        def load(pack_id: str, version: str) -> RulePackVersion:
            row = session.get(
                RulePackVersion,
                {"pack_id": pack_id, "version": version},
            )
            if row is None or row.status != "installed":
                raise AddonError(
                    f"addon rule dependency is not installed: {pack_id}@{version}"
                )
            pack = session.get(RulePack, pack_id)
            if pack is None or pack.system_id != campaign.system_id:
                raise AddonError(
                    f"addon rule dependency is incompatible with campaign system: "
                    f"{pack_id}@{version}"
                )
            supported = {str(item) for item in row.manifest.get("editions", [])}
            if supported and edition and edition not in supported:
                raise AddonError(
                    f"addon rule dependency {pack_id}@{version} does not support "
                    f"campaign edition {edition}"
                )
            selected_identity = (row.version, row.checksum)
            existing_identity = selected.get(pack_id)
            if existing_identity is not None and existing_identity != selected_identity:
                raise AddonError(
                    f"addon rule dependency requirements are ambiguous for {pack_id}"
                )
            selected[pack_id] = selected_identity
            if len(selected) > _MAX_ADDON_RULE_DEPENDENCY_PACKS:
                raise AddonError(
                    "addon rule dependency closure exceeds the safe pack limit "
                    f"of {_MAX_ADDON_RULE_DEPENDENCY_PACKS}"
                )
            return row

        for component in components:
            if str(component.get("kind") or "") != "rule_pack":
                continue
            root_id = str(component["id"])
            root_version = str(component["version"])
            stack: list[tuple[str, str, int, bool]] = [
                (root_id, root_version, 0, False)
            ]
            visiting: list[str] = []
            while stack:
                pack_id, version, depth, exiting = stack.pop()
                if exiting:
                    if visiting[-1] != pack_id:
                        raise AddonError("addon rule dependency traversal state is invalid")
                    visiting.pop()
                    visited.add(pack_id)
                    continue
                if pack_id in visiting:
                    cycle = " -> ".join((*visiting[visiting.index(pack_id) :], pack_id))
                    raise AddonError(f"addon rule dependency cycle: {cycle}")
                row = load(pack_id, version)
                if pack_id in visited:
                    continue
                if depth > _MAX_ADDON_RULE_DEPENDENCY_DEPTH:
                    raise AddonError(
                        "addon rule dependency closure exceeds the safe depth limit "
                        f"of {_MAX_ADDON_RULE_DEPENDENCY_DEPTH}"
                    )
                visiting.append(pack_id)
                stack.append((pack_id, version, depth, True))
                dependency_ids: set[str] = set()
                child_frames: list[tuple[str, str, int, bool]] = []
                for dependency in row.manifest.get("dependencies", []):
                    if not isinstance(dependency, dict):
                        raise AddonError(
                            f"addon rule dependency {pack_id} must pin exact version and checksum"
                        )
                    dependency_id = str(dependency.get("id") or "")
                    dependency_version = str(dependency.get("version") or "")
                    expected_checksum = str(dependency.get("checksum") or "")
                    if not dependency_id or not dependency_version or not expected_checksum:
                        raise AddonError(
                            f"addon rule dependency {pack_id} must pin exact version and checksum"
                        )
                    if dependency_id in dependency_ids:
                        raise AddonError(
                            "addon rule dependency requirements are ambiguous for "
                            f"{dependency_id}"
                        )
                    dependency_ids.add(dependency_id)
                    dependency_row = load(dependency_id, dependency_version)
                    definition_checksum = str(
                        dict(dependency_row.provenance or {})
                        .get("content_definition", {})
                        .get("definition_checksum")
                        or ""
                    )
                    if expected_checksum not in {
                        dependency_row.checksum,
                        definition_checksum,
                    }:
                        raise AddonError(
                            f"addon rule dependency {pack_id} requires checksum "
                            f"{expected_checksum} for {dependency_id}@{dependency_version}"
                        )
                    if dependency_id not in root_ids:
                        dependencies[dependency_id] = {
                            "kind": "rule_pack",
                            "id": dependency_id,
                            "version": dependency_row.version,
                            "checksum": dependency_row.checksum,
                            "optional": False,
                        }
                    child_frames.append(
                        (dependency_id, dependency_version, depth + 1, False)
                    )
                stack.extend(reversed(child_frames))
        return [dependencies[pack_id] for pack_id in sorted(dependencies)]

    @staticmethod
    def _assert_addon_conflicts(
        session,
        *,
        campaign_id: str,
        branch_id: str,
        addon_id: str,
        manifest: dict[str, Any],
        enabled: bool,
    ) -> None:
        if not enabled:
            return
        conflicts = {
            str(item.get("id") if isinstance(item, dict) else item)
            for item in manifest.get("conflicts", [])
        }
        reverse_conflicts = set()
        active = list(
            session.scalars(
                select(CampaignAddonActivation).where(
                    CampaignAddonActivation.campaign_id == campaign_id,
                    CampaignAddonActivation.branch_id == branch_id,
                    CampaignAddonActivation.enabled.is_(True),
                    CampaignAddonActivation.addon_id != addon_id,
                )
            )
        )
        for activation in active:
            other = session.get(
                ContentAddonVersion,
                {"addon_id": activation.addon_id, "version": activation.version},
            )
            if other is not None and addon_id in {
                str(item.get("id") if isinstance(item, dict) else item)
                for item in other.manifest.get("conflicts", [])
            }:
                reverse_conflicts.add(activation.addon_id)
        collisions = sorted({item.addon_id for item in active} & conflicts | reverse_conflicts)
        if collisions:
            raise AddonError("addon conflicts with: " + ", ".join(collisions))

    @staticmethod
    def _set_rule_component_ownership(
        session,
        *,
        campaign: Campaign,
        branch_id: str,
        addon_id: str,
        components: list[dict[str, Any]],
        enabled: bool,
        addon_options: dict[str, Any],
    ) -> None:
        rule_options = dict(addon_options.get("rule_options") or {})
        if set(addon_options) - {"rule_options"}:
            raise AddonError("addon activation options support only rule_options")
        rule_component_ids = {
            str(component["id"]) for component in components if component["kind"] == "rule_pack"
        }
        unknown_rule_options = sorted(set(rule_options) - rule_component_ids)
        if unknown_rule_options:
            raise AddonError(
                "addon rule_options reference unknown rule components: "
                + ", ".join(unknown_rule_options)
            )
        if any(not isinstance(value, dict) for value in rule_options.values()):
            raise AddonError("addon rule_options values must be objects")
        for component in components:
            if component["kind"] != "rule_pack":
                continue
            version = session.get(
                RulePackVersion,
                {"pack_id": component["id"], "version": component["version"]},
            )
            if version is None or version.status != "installed":
                raise AddonError(f"addon rule component is not installed: {component['id']}")
            row = session.get(
                CampaignRuleActivation,
                {
                    "campaign_id": campaign.id,
                    "branch_id": branch_id,
                    "pack_id": component["id"],
                },
            )
            if row is None:
                row = CampaignRuleActivation(
                    campaign_id=campaign.id,
                    branch_id=branch_id,
                    pack_id=component["id"],
                    version=version.version,
                    checksum=version.checksum,
                    enabled=False,
                    options={},
                )
                session.add(row)
            elif row.version != version.version or row.checksum != version.checksum:
                if row.enabled:
                    raise AddonError(
                        f"campaign has a different lock for addon rule {component['id']}"
                    )
                row.version = version.version
                row.checksum = version.checksum
            stored = dict(row.options or {})
            owners = {str(item) for item in stored.pop("_addon_ids", []) if str(item)}
            manual_owner = bool(stored.pop("_manual_activation_preserved", False))
            raw_owner_options = stored.pop("_addon_rule_options", {})
            if not isinstance(raw_owner_options, dict) or any(
                not isinstance(value, dict) for value in raw_owner_options.values()
            ):
                raise AddonError("stored addon rule option ownership is invalid")
            owner_options = {
                str(owner): dict(value) for owner, value in raw_owner_options.items() if str(owner)
            }
            raw_manual_options = stored.pop("_manual_options", {})
            if not isinstance(raw_manual_options, dict):
                raise AddonError("stored manual rule options are invalid")
            manual_options = dict(raw_manual_options)
            if owners and not owner_options and stored:
                raise AddonError(
                    "stored addon rule options predate owner accounting; "
                    "reinstall the affected addon release"
                )
            if enabled:
                if row.enabled and not owners:
                    manual_owner = True
                    manual_options = dict(stored)
                owners.add(addon_id)
                owner_options[addon_id] = dict(rule_options.get(component["id"], {}))
            else:
                owners.discard(addon_id)
                owner_options.pop(addon_id, None)
            effective_options = dict(manual_options) if manual_owner else {}
            option_sources = {key: "manual activation" for key in effective_options}
            for owner in sorted(owners):
                requested = dict(owner_options.get(owner) or {})
                conflicting = {
                    key
                    for key, value in requested.items()
                    if key in effective_options and effective_options[key] != value
                }
                if conflicting:
                    details = ", ".join(
                        f"{key} ({option_sources[key]} vs {owner})" for key in sorted(conflicting)
                    )
                    raise AddonError(
                        f"addon rule options conflict for {component['id']}: " + details
                    )
                effective_options.update(requested)
                option_sources.update({key: owner for key in requested})
            row.enabled = bool(owners or manual_owner)
            row.options = {
                **effective_options,
                "_addon_ids": sorted(owners),
                "_manual_activation_preserved": manual_owner,
                "_addon_rule_options": {
                    owner: dict(owner_options.get(owner) or {}) for owner in sorted(owners)
                },
                "_manual_options": manual_options if manual_owner else {},
            }

    @staticmethod
    def _version_info(row: ContentAddonVersion, addon: ContentAddon) -> AddonVersionInfo:
        return AddonVersionInfo(
            addon_id=row.addon_id,
            version=row.version,
            checksum=row.checksum,
            status=row.status,
            system_id=addon.system_id,
            title=str(dict(row.manifest or {}).get("title") or addon.title),
            manifest=dict(row.manifest or {}),
            components=tuple(dict(item) for item in row.components or []),
            provenance=dict(row.provenance or {}),
            validation_report=dict(row.validation_report or {}),
        )

    @staticmethod
    def _activation_info(row: CampaignAddonActivation) -> AddonActivationInfo:
        return AddonActivationInfo(
            campaign_id=row.campaign_id,
            branch_id=row.branch_id,
            addon_id=row.addon_id,
            version=row.version,
            checksum=row.checksum,
            enabled=row.enabled,
            component_locks=tuple(dict(item) for item in row.component_locks or []),
            options=dict(row.options or {}),
        )
