"""Durable, reviewable import lifecycles shared by rulebooks and modules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from sagasmith_core.database import Database
from sagasmith_core.idempotency import IdempotencyService, IdempotencyWrite
from sagasmith_core.models import Campaign, ImportJob


class ImportJobError(ValueError):
    """Raised when an import job is malformed or moved to an invalid state."""


_KINDS = {"rulebook", "module"}
_STATES = {
    "staged",
    "inspected",
    "extracted",
    "review_required",
    "reviewed",
    "compiled",
    "validated",
    "installed",
    "imported",
    "activated",
    "failed",
}
_TRANSITIONS = {
    "staged": {"inspected", "failed"},
    # Candidate extraction and entering review are one atomic public operation
    # when candidates are found; an empty extraction remains ``extracted``.
    # Evidence-bound text revisions rerun inspection without indexing or
    # mutating the staged artifact.
    "inspected": {"inspected", "extracted", "review_required", "validated", "failed"},
    "extracted": {"review_required", "failed"},
    # A source-bound Agent may add missed semantic entities before any approval.
    # Replacing the candidate set keeps the job in review and invalidates drafts.
    # Candidate decisions remain editable until an explicit finalization call.
    # Merely assigning accepted/rejected dispositions must not freeze a draft.
    "review_required": {"review_required", "reviewed", "failed"},
    "reviewed": {"compiled", "failed"},
    # A validated but not-yet-installed draft may be reopened when improved
    # extraction or review evidence becomes available. Installed versions stay
    # immutable and require a new version/job instead.
    "compiled": {"extracted", "review_required", "installed", "failed"},
    "installed": {"activated", "failed"},
    "validated": {"imported", "failed"},
    # An inactive imported module remains an editable workspace until its
    # portable package is explicitly finalized. Finalization records the
    # immutable package and moves the job to the shared compiled state.
    "imported": {"inspected", "imported", "compiled", "activated", "failed"},
    "activated": set(),
    "failed": {"inspected", "extracted", "validated", "compiled", "failed"},
}


@dataclass(frozen=True)
class ImportJobInfo:
    id: str
    campaign_id: str
    system_id: str
    kind: str
    state: str
    artifact: str
    artifact_checksum: str
    source_id: str | None
    module_id: str | None
    parser_profile: str
    parser_version: str
    payload: dict[str, Any]
    inspection: dict[str, Any]
    candidates: list[dict[str, Any]]
    validation: dict[str, Any]
    result: dict[str, Any]
    error: str
    revision: int


class ImportJobService:
    """Store import evidence separately from mutable source and campaign rows."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        campaign_id: str,
        kind: str,
        artifact: str,
        artifact_checksum: str = "",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        if kind not in _KINDS:
            raise ImportJobError(f"unsupported import kind: {kind}")
        if not artifact.strip():
            raise ImportJobError("import artifact is required")
        with self.database.transaction() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise LookupError(campaign_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            row = ImportJob(
                id=str(uuid4()),
                campaign_id=campaign_id,
                system_id=campaign.system_id,
                kind=kind,
                artifact=artifact,
                artifact_checksum=artifact_checksum,
                payload=dict(payload or {}),
            )
            session.add(row)
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def get(self, job_id: str) -> ImportJobInfo:
        with self.database.transaction() as session:
            row = session.get(ImportJob, job_id)
            if row is None:
                raise LookupError(job_id)
            return self._info(row)

    def list(self, campaign_id: str, *, kind: str | None = None) -> list[ImportJobInfo]:
        statement = select(ImportJob).where(ImportJob.campaign_id == campaign_id)
        if kind is not None:
            statement = statement.where(ImportJob.kind == kind)
        statement = statement.order_by(ImportJob.updated_at.desc(), ImportJob.id.desc())
        with self.database.transaction() as session:
            return [self._info(row) for row in session.scalars(statement)]

    def record_inspection(
        self,
        job_id: str,
        inspection: dict[str, Any],
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        return self._update(
            job_id,
            state="inspected",
            inspection=dict(inspection),
            parser_profile=str(inspection.get("parser_profile") or ""),
            parser_version=str(inspection.get("parser_version") or ""),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def set_candidates(
        self,
        job_id: str,
        candidates: list[dict[str, Any]],
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, candidate in enumerate(candidates):
            value = dict(candidate)
            candidate_id = str(value.get("id") or "").strip()
            if not candidate_id:
                raise ImportJobError(f"candidates[{index}].id is required")
            if candidate_id in seen:
                raise ImportJobError("candidate ids must be unique")
            seen.add(candidate_id)
            status = str(value.get("review_status") or "pending")
            if status not in {"pending", "accepted", "rejected", "needs_revision"}:
                raise ImportJobError(f"candidates[{index}].review_status is invalid")
            value["review_status"] = status
            value.setdefault("original_fingerprint", self._candidate_fingerprint(value))
            value.setdefault("edit_history", [])
            value["draft_state"] = "editing"
            normalized.append(value)
        return self._update(
            job_id,
            state="review_required" if normalized else "extracted",
            candidates=normalized,
            # Candidate replacement invalidates any earlier compiled draft.
            # The state transition prevents installation until a fresh review
            # and compile have succeeded.
            validation={},
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def review_candidates(
        self,
        job_id: str,
        decisions: list[dict[str, Any]],
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        with self.database.transaction() as session:
            row = self._row(session, job_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            if expected_revision is not None and row.revision != expected_revision:
                raise ImportJobError(
                    "import job revision conflict: "
                    f"expected {expected_revision}, found {row.revision}"
                )
            values = [dict(item) for item in row.candidates or []]
            by_id = {str(item.get("id")): item for item in values}
            for decision in decisions:
                candidate_id = str(decision.get("id") or "").strip()
                if candidate_id not in by_id:
                    raise ImportJobError(f"unknown candidate: {candidate_id}")
                status = str(decision.get("review_status") or "").strip()
                if status not in {"pending", "accepted", "rejected", "needs_revision"}:
                    raise ImportJobError(
                        "review_status must be pending, accepted, rejected, or needs_revision"
                    )
                candidate = by_id[candidate_id]
                before_fingerprint = self._candidate_fingerprint(candidate)
                candidate["review_status"] = status
                if "artifact" in decision:
                    artifact = decision["artifact"]
                    if not isinstance(artifact, dict):
                        raise ImportJobError("candidate artifact must be an object")
                    candidate["artifact"] = dict(artifact)
                if "note" in decision:
                    candidate["review_note"] = str(decision["note"])
                if "draft_issues" in decision:
                    issues = decision["draft_issues"]
                    if not isinstance(issues, list) or any(
                        not isinstance(item, dict) for item in issues
                    ):
                        raise ImportJobError("candidate draft_issues must be an array of objects")
                    candidate["draft_issues"] = [dict(item) for item in issues]
                if "disposition" in decision:
                    disposition = str(decision["disposition"] or "")
                    if disposition not in {"include", "exclude", "unresolved"}:
                        raise ImportJobError(
                            "candidate disposition must be include, exclude, or unresolved"
                        )
                    candidate["disposition"] = disposition
                candidate["draft_state"] = "editing"
                after_fingerprint = self._candidate_fingerprint(candidate)
                history = list(candidate.get("edit_history") or [])
                history.append(
                    {
                        "schema_version": 1,
                        "revision": row.revision + 1,
                        "editor": str(decision.get("editor") or "agent")[:200],
                        "operation": str(decision.get("operation") or "edit")[:100],
                        "note": str(decision.get("note") or "")[:2000],
                        "before_fingerprint": before_fingerprint,
                        "after_fingerprint": after_fingerprint,
                    }
                )
                candidate["edit_history"] = history
            row.candidates = values
            # Accepted/rejected are editable draft dispositions. Only
            # finalize_candidate_review may cross into the frozen reviewed state.
            next_state = "review_required"
            self._require_transition(row.state, next_state)
            row.state = next_state
            row.validation = {}
            result_value = dict(row.result or {})
            result_value.pop("review_finalization", None)
            row.result = result_value
            row.revision += 1
            row.error = ""
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=row.campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def finalize_candidate_review(
        self,
        job_id: str,
        *,
        candidates: list[dict[str, Any]] | None = None,
        confirmation: dict[str, Any],
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        """Freeze one completely dispositioned candidate draft for compilation."""

        if not isinstance(confirmation, dict) or not confirmation:
            raise ImportJobError("candidate review finalization requires confirmation metadata")
        with self.database.transaction() as session:
            row = self._row(session, job_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            if expected_revision is not None and row.revision != expected_revision:
                raise ImportJobError(
                    "import job revision conflict: "
                    f"expected {expected_revision}, found {row.revision}"
                )
            if row.state != "review_required":
                raise ImportJobError("only an editable candidate review may be finalized")
            values = [dict(item) for item in (candidates or row.candidates or [])]
            current_ids = [str(item.get("id") or "") for item in row.candidates or []]
            final_ids = [str(item.get("id") or "") for item in values]
            if not values or final_ids != current_ids or len(set(final_ids)) != len(final_ids):
                raise ImportJobError(
                    "finalized candidates must preserve the complete ordered draft identity set"
                )
            incomplete = [
                candidate_id
                for candidate_id, value in zip(final_ids, values, strict=True)
                if value.get("review_status") not in {"accepted", "rejected"}
            ]
            if incomplete:
                raise ImportJobError(
                    "candidate review cannot be finalized with unresolved dispositions: "
                    + ", ".join(incomplete)
                )
            for value in values:
                value["draft_state"] = "finalized"
            self._require_transition(row.state, "reviewed")
            row.candidates = values
            row.state = "reviewed"
            row.revision += 1
            row.error = ""
            result_value = dict(row.result or {})
            result_value["review_finalization"] = {
                **dict(confirmation),
                "candidate_revision": row.revision,
                "candidate_set_fingerprint": self._candidate_fingerprint(values),
            }
            row.result = result_value
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=row.campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    def record_validation(
        self,
        job_id: str,
        validation: dict[str, Any],
        *,
        state: str = "validated",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        if state not in {"compiled", "validated", "installed", "imported", "activated", "failed"}:
            raise ImportJobError(f"invalid validation state: {state}")
        return self._update(
            job_id,
            state=state,
            validation=dict(validation),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def record_result(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        state: str,
        source_id: str | None = None,
        module_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        return self._update(
            job_id,
            state=state,
            result=dict(result),
            source_id=source_id,
            module_id=module_id,
            error="",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
    ) -> ImportJobInfo:
        return self._update(
            job_id,
            state="failed",
            error=str(error),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            idempotency_write=idempotency_write,
        )

    def _update(
        self,
        job_id: str,
        *,
        state: str,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        idempotency_write: IdempotencyWrite | None = None,
        **fields: Any,
    ) -> ImportJobInfo:
        if state not in _STATES:
            raise ImportJobError(f"invalid import state: {state}")
        with self.database.transaction() as session:
            row = self._row(session, job_id)
            idempotency = IdempotencyService(self.database)
            idempotency.require_uncommitted_in_session(
                session,
                idempotency_key,
                idempotency_write,
            )
            if expected_revision is not None and row.revision != expected_revision:
                raise ImportJobError(
                    "import job revision conflict: "
                    f"expected {expected_revision}, found {row.revision}"
                )
            self._require_transition(row.state, state)
            for key, value in fields.items():
                setattr(row, key, value)
            if state == "review_required" and "result" not in fields:
                result_value = dict(row.result or {})
                result_value.pop("review_finalization", None)
                row.result = result_value
            row.state = state
            row.revision += 1
            if state != "failed" and "error" not in fields:
                row.error = ""
            session.flush()
            result = self._info(row)
            idempotency.remember_write_in_session(
                session,
                campaign_id=row.campaign_id,
                key=idempotency_key,
                write=idempotency_write,
                result=result,
            )
            return result

    @staticmethod
    def _row(session: Any, job_id: str) -> ImportJob:
        row = session.get(ImportJob, job_id)
        if row is None:
            raise LookupError(job_id)
        return row

    @staticmethod
    def _require_transition(current: str, target: str) -> None:
        if current == target:
            return
        if target not in _TRANSITIONS.get(current, set()):
            raise ImportJobError(f"invalid import job transition: {current} -> {target}")

    @staticmethod
    def _info(row: ImportJob) -> ImportJobInfo:
        return ImportJobInfo(
            id=row.id,
            campaign_id=row.campaign_id,
            system_id=row.system_id,
            kind=row.kind,
            state=row.state,
            artifact=row.artifact,
            artifact_checksum=row.artifact_checksum,
            source_id=row.source_id,
            module_id=row.module_id,
            parser_profile=row.parser_profile,
            parser_version=row.parser_version,
            payload=dict(row.payload or {}),
            inspection=dict(row.inspection or {}),
            candidates=[dict(item) for item in row.candidates or []],
            validation=dict(row.validation or {}),
            result=dict(row.result or {}),
            error=row.error,
            revision=row.revision,
        )

    @staticmethod
    def _candidate_fingerprint(value: Any) -> str:
        """Hash one draft value without recursively hashing its edit ledger."""

        def semantic(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    str(key): semantic(child)
                    for key, child in item.items()
                    if str(key) not in {"edit_history", "original_fingerprint"}
                }
            if isinstance(item, list):
                return [semantic(child) for child in item]
            return item

        encoded = json.dumps(
            semantic(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
