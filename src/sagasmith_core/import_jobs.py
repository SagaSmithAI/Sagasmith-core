"""Durable, reviewable import lifecycles shared by rulebooks and modules."""

from __future__ import annotations

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
    "inspected": {"extracted", "review_required", "validated", "failed"},
    "extracted": {"review_required", "failed"},
    "review_required": {"reviewed", "failed"},
    "reviewed": {"compiled", "failed"},
    # A validated but not-yet-installed draft may be reopened when improved
    # extraction or review evidence becomes available. Installed versions stay
    # immutable and require a new version/job instead.
    "compiled": {"extracted", "review_required", "installed", "failed"},
    "installed": {"activated", "failed"},
    "validated": {"imported", "failed"},
    "imported": {"activated", "failed"},
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
                if status not in {"accepted", "rejected", "needs_revision"}:
                    raise ImportJobError(
                        "review_status must be accepted, rejected, or needs_revision"
                    )
                candidate = by_id[candidate_id]
                candidate["review_status"] = status
                if "artifact" in decision:
                    artifact = decision["artifact"]
                    if not isinstance(artifact, dict):
                        raise ImportJobError("candidate artifact must be an object")
                    candidate["artifact"] = dict(artifact)
                if "note" in decision:
                    candidate["review_note"] = str(decision["note"])
            row.candidates = values
            next_state = (
                "reviewed"
                if values
                and all(item.get("review_status") in {"accepted", "rejected"} for item in values)
                else "review_required"
            )
            self._require_transition(row.state, next_state)
            row.state = next_state
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
            raise ImportJobError(
                f"invalid import job transition: {current} -> {target}"
            )

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
