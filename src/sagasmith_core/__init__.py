"""Compatibility exports; prefer contracts and the service-specific modules."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sagasmith_core.access import AccessDeniedError, AccessService, default_local_principal
    from sagasmith_core.actor_lifecycle import (
        ActorLifecycleResult,
        ActorLifecycleService,
        InitialActorGrant,
    )
    from sagasmith_core.addons import AddonError, AddonService
    from sagasmith_core.auth_context import (
        AUTH_CONTEXT_DELEGATION_SCHEMA,
        AUTH_CONTEXT_META_KEY,
        AUTH_CONTEXT_RECEIPT_META_KEY,
        AUTH_CONTEXT_SCHEMA,
        AuthContext,
        AuthContextNonceGuard,
        sign_auth_context,
        sign_delegated_auth_context,
        verify_auth_context,
    )
    from sagasmith_core.branches import BranchService
    from sagasmith_core.campaigns import CampaignService
    from sagasmith_core.characters import CharacterService
    from sagasmith_core.content_pack import ACTOR_CARD_SCHEMA as CONTENT_ACTOR_CARD_SCHEMA
    from sagasmith_core.content_pack import (
        CONTENT_PACKAGE_FORMAT,
        CONTENT_PACKAGE_SCHEMA_VERSION,
        ContentPackageError,
        blob_descriptor,
        build_content_package,
        build_source_bundle,
        content_package_checksum,
        dumps_content_archive,
        loads_content_archive,
        source_ref,
        validate_content_package,
    )
    from sagasmith_core.content_pack import build_actor_card as build_content_actor_card
    from sagasmith_core.content_pack import validate_actor_card as validate_content_actor_card
    from sagasmith_core.continuity import ContinuityService
    from sagasmith_core.continuity_commit import FACT_KEY_WRITE_ACTIONS, ContinuityCommitService
    from sagasmith_core.database import Database
    from sagasmith_core.documents import (
        DOCUMENT_NORMALIZER_VERSION,
        DOCUMENT_SOURCE_SUFFIXES,
        GENERIC_DOCUMENT_LAYOUT_PROFILE,
        CascadingOcrProvider,
        DocumentLayoutProfile,
        DocumentQualityError,
        NormalizedDocument,
        OcrPageLayout,
        OcrTextBlock,
        PageLocator,
        PdfDocumentConverter,
        PdfTextLayoutProvider,
        RapidOcrProvider,
        RenderedDocumentPage,
        apply_document_page_revisions,
        extract_pdf_page_text,
        file_sha256,
        normalize_document,
        normalized_document_page_text,
        ocr_layout_text,
        render_pdf_page,
    )
    from sagasmith_core.embeddings import (
        BgeEmbedder,
        BgeM3Embedder,
        BgeSmallEnEmbedder,
        BgeSmallZhEmbedder,
        EmbeddingProfile,
        configured_profiles,
        create_embedder,
    )
    from sagasmith_core.events import EventService
    from sagasmith_core.idempotency import (
        IdempotencyConflictError,
        IdempotencyService,
        IdempotencyWrite,
        request_hash,
    )
    from sagasmith_core.import_jobs import ImportJobError, ImportJobService
    from sagasmith_core.knowledge import ActorKnowledgeService
    from sagasmith_core.memory import MemoryService, validate_subject_context_fact
    from sagasmith_core.modules import (
        EXACT_MODULE_SOURCE_FIELD_ORDER,
        EXACT_MODULE_SOURCE_FIELDS,
        MANAGED_MODULE_SOURCE_FIELDS,
        ModuleService,
        clean_source_evidence_text,
        normalize_source_evidence_text,
    )
    from sagasmith_core.revisions import RevisionService
    from sagasmith_core.rule_packs import RulePackService
    from sagasmith_core.rule_profiles import RuleProfileService
    from sagasmith_core.rule_receipts import RuleReceiptService
    from sagasmith_core.rules import RuleService
    from sagasmith_core.snapshots import SnapshotService
    from sagasmith_core.state import (
        ActorKnowledgeTransfer,
        CharacterStateUpdate,
        StateMutationService,
    )
    from sagasmith_core.subject_context import SubjectContextService
    from sagasmith_core.systems import SystemDefinition, SystemRegistry
    from sagasmith_core.vector import VectorStore
    from sagasmith_core.vector_jobs import VectorFlushResult, VectorIndexJobService

_EXPORTS = {
    "AccessDeniedError": ("sagasmith_core.access", "AccessDeniedError"),
    "AccessService": ("sagasmith_core.access", "AccessService"),
    "default_local_principal": ("sagasmith_core.access", "default_local_principal"),
    "ActorLifecycleResult": ("sagasmith_core.actor_lifecycle", "ActorLifecycleResult"),
    "ActorLifecycleService": ("sagasmith_core.actor_lifecycle", "ActorLifecycleService"),
    "InitialActorGrant": ("sagasmith_core.actor_lifecycle", "InitialActorGrant"),
    "AddonError": ("sagasmith_core.addons", "AddonError"),
    "AddonService": ("sagasmith_core.addons", "AddonService"),
    "AUTH_CONTEXT_DELEGATION_SCHEMA": (
        "sagasmith_core.auth_context",
        "AUTH_CONTEXT_DELEGATION_SCHEMA",
    ),
    "AUTH_CONTEXT_META_KEY": ("sagasmith_core.auth_context", "AUTH_CONTEXT_META_KEY"),
    "AUTH_CONTEXT_RECEIPT_META_KEY": (
        "sagasmith_core.auth_context",
        "AUTH_CONTEXT_RECEIPT_META_KEY",
    ),
    "AUTH_CONTEXT_SCHEMA": ("sagasmith_core.auth_context", "AUTH_CONTEXT_SCHEMA"),
    "AuthContext": ("sagasmith_core.auth_context", "AuthContext"),
    "AuthContextNonceGuard": ("sagasmith_core.auth_context", "AuthContextNonceGuard"),
    "sign_auth_context": ("sagasmith_core.auth_context", "sign_auth_context"),
    "sign_delegated_auth_context": ("sagasmith_core.auth_context", "sign_delegated_auth_context"),
    "verify_auth_context": ("sagasmith_core.auth_context", "verify_auth_context"),
    "BranchService": ("sagasmith_core.branches", "BranchService"),
    "CampaignService": ("sagasmith_core.campaigns", "CampaignService"),
    "CharacterService": ("sagasmith_core.characters", "CharacterService"),
    "CONTENT_ACTOR_CARD_SCHEMA": ("sagasmith_core.content_pack", "ACTOR_CARD_SCHEMA"),
    "CONTENT_PACKAGE_FORMAT": ("sagasmith_core.content_pack", "CONTENT_PACKAGE_FORMAT"),
    "CONTENT_PACKAGE_SCHEMA_VERSION": (
        "sagasmith_core.content_pack",
        "CONTENT_PACKAGE_SCHEMA_VERSION",
    ),
    "ContentPackageError": ("sagasmith_core.content_pack", "ContentPackageError"),
    "blob_descriptor": ("sagasmith_core.content_pack", "blob_descriptor"),
    "build_content_package": ("sagasmith_core.content_pack", "build_content_package"),
    "build_source_bundle": ("sagasmith_core.content_pack", "build_source_bundle"),
    "content_package_checksum": ("sagasmith_core.content_pack", "content_package_checksum"),
    "dumps_content_archive": ("sagasmith_core.content_pack", "dumps_content_archive"),
    "loads_content_archive": ("sagasmith_core.content_pack", "loads_content_archive"),
    "source_ref": ("sagasmith_core.content_pack", "source_ref"),
    "validate_content_package": ("sagasmith_core.content_pack", "validate_content_package"),
    "build_content_actor_card": ("sagasmith_core.content_pack", "build_actor_card"),
    "validate_content_actor_card": ("sagasmith_core.content_pack", "validate_actor_card"),
    "ContinuityService": ("sagasmith_core.continuity", "ContinuityService"),
    "FACT_KEY_WRITE_ACTIONS": ("sagasmith_core.continuity_commit", "FACT_KEY_WRITE_ACTIONS"),
    "ContinuityCommitService": ("sagasmith_core.continuity_commit", "ContinuityCommitService"),
    "Database": ("sagasmith_core.database", "Database"),
    "DOCUMENT_NORMALIZER_VERSION": ("sagasmith_core.documents", "DOCUMENT_NORMALIZER_VERSION"),
    "DOCUMENT_SOURCE_SUFFIXES": ("sagasmith_core.documents", "DOCUMENT_SOURCE_SUFFIXES"),
    "GENERIC_DOCUMENT_LAYOUT_PROFILE": (
        "sagasmith_core.documents",
        "GENERIC_DOCUMENT_LAYOUT_PROFILE",
    ),
    "CascadingOcrProvider": ("sagasmith_core.documents", "CascadingOcrProvider"),
    "DocumentLayoutProfile": ("sagasmith_core.documents", "DocumentLayoutProfile"),
    "DocumentQualityError": ("sagasmith_core.documents", "DocumentQualityError"),
    "NormalizedDocument": ("sagasmith_core.documents", "NormalizedDocument"),
    "OcrPageLayout": ("sagasmith_core.documents", "OcrPageLayout"),
    "OcrTextBlock": ("sagasmith_core.documents", "OcrTextBlock"),
    "PageLocator": ("sagasmith_core.documents", "PageLocator"),
    "PdfDocumentConverter": ("sagasmith_core.documents", "PdfDocumentConverter"),
    "PdfTextLayoutProvider": ("sagasmith_core.documents", "PdfTextLayoutProvider"),
    "RapidOcrProvider": ("sagasmith_core.documents", "RapidOcrProvider"),
    "RenderedDocumentPage": ("sagasmith_core.documents", "RenderedDocumentPage"),
    "apply_document_page_revisions": ("sagasmith_core.documents", "apply_document_page_revisions"),
    "extract_pdf_page_text": ("sagasmith_core.documents", "extract_pdf_page_text"),
    "file_sha256": ("sagasmith_core.documents", "file_sha256"),
    "normalize_document": ("sagasmith_core.documents", "normalize_document"),
    "normalized_document_page_text": ("sagasmith_core.documents", "normalized_document_page_text"),
    "ocr_layout_text": ("sagasmith_core.documents", "ocr_layout_text"),
    "render_pdf_page": ("sagasmith_core.documents", "render_pdf_page"),
    "BgeEmbedder": ("sagasmith_core.embeddings", "BgeEmbedder"),
    "BgeM3Embedder": ("sagasmith_core.embeddings", "BgeM3Embedder"),
    "BgeSmallEnEmbedder": ("sagasmith_core.embeddings", "BgeSmallEnEmbedder"),
    "BgeSmallZhEmbedder": ("sagasmith_core.embeddings", "BgeSmallZhEmbedder"),
    "EmbeddingProfile": ("sagasmith_core.embeddings", "EmbeddingProfile"),
    "configured_profiles": ("sagasmith_core.embeddings", "configured_profiles"),
    "create_embedder": ("sagasmith_core.embeddings", "create_embedder"),
    "EventService": ("sagasmith_core.events", "EventService"),
    "IdempotencyConflictError": ("sagasmith_core.idempotency", "IdempotencyConflictError"),
    "IdempotencyService": ("sagasmith_core.idempotency", "IdempotencyService"),
    "IdempotencyWrite": ("sagasmith_core.idempotency", "IdempotencyWrite"),
    "request_hash": ("sagasmith_core.idempotency", "request_hash"),
    "ImportJobError": ("sagasmith_core.import_jobs", "ImportJobError"),
    "ImportJobService": ("sagasmith_core.import_jobs", "ImportJobService"),
    "ActorKnowledgeService": ("sagasmith_core.knowledge", "ActorKnowledgeService"),
    "MemoryService": ("sagasmith_core.memory", "MemoryService"),
    "validate_subject_context_fact": ("sagasmith_core.memory", "validate_subject_context_fact"),
    "EXACT_MODULE_SOURCE_FIELD_ORDER": (
        "sagasmith_core.modules",
        "EXACT_MODULE_SOURCE_FIELD_ORDER",
    ),
    "EXACT_MODULE_SOURCE_FIELDS": ("sagasmith_core.modules", "EXACT_MODULE_SOURCE_FIELDS"),
    "MANAGED_MODULE_SOURCE_FIELDS": ("sagasmith_core.modules", "MANAGED_MODULE_SOURCE_FIELDS"),
    "ModuleService": ("sagasmith_core.modules", "ModuleService"),
    "clean_source_evidence_text": ("sagasmith_core.modules", "clean_source_evidence_text"),
    "normalize_source_evidence_text": ("sagasmith_core.modules", "normalize_source_evidence_text"),
    "RevisionService": ("sagasmith_core.revisions", "RevisionService"),
    "RulePackService": ("sagasmith_core.rule_packs", "RulePackService"),
    "RuleProfileService": ("sagasmith_core.rule_profiles", "RuleProfileService"),
    "RuleReceiptService": ("sagasmith_core.rule_receipts", "RuleReceiptService"),
    "RuleService": ("sagasmith_core.rules", "RuleService"),
    "SnapshotService": ("sagasmith_core.snapshots", "SnapshotService"),
    "ActorKnowledgeTransfer": ("sagasmith_core.state", "ActorKnowledgeTransfer"),
    "CharacterStateUpdate": ("sagasmith_core.state", "CharacterStateUpdate"),
    "StateMutationService": ("sagasmith_core.state", "StateMutationService"),
    "SubjectContextService": ("sagasmith_core.subject_context", "SubjectContextService"),
    "SystemDefinition": ("sagasmith_core.systems", "SystemDefinition"),
    "SystemRegistry": ("sagasmith_core.systems", "SystemRegistry"),
    "VectorStore": ("sagasmith_core.vector", "VectorStore"),
    "VectorFlushResult": ("sagasmith_core.vector_jobs", "VectorFlushResult"),
    "VectorIndexJobService": ("sagasmith_core.vector_jobs", "VectorIndexJobService"),
    "UnitOfWork": ("sagasmith_core.database", "UnitOfWork"),
    "IdempotencyIdentity": ("sagasmith_core.idempotency", "IdempotencyIdentity"),
}

__all__ = [
    "DOCUMENT_NORMALIZER_VERSION",
    "DOCUMENT_SOURCE_SUFFIXES",
    "EXACT_MODULE_SOURCE_FIELD_ORDER",
    "EXACT_MODULE_SOURCE_FIELDS",
    "BgeEmbedder",
    "BgeM3Embedder",
    "BgeSmallEnEmbedder",
    "BgeSmallZhEmbedder",
    "ActorKnowledgeService",
    "ActorLifecycleResult",
    "ActorLifecycleService",
    "AddonError",
    "AddonService",
    "AccessDeniedError",
    "AccessService",
    "AUTH_CONTEXT_META_KEY",
    "AUTH_CONTEXT_RECEIPT_META_KEY",
    "AUTH_CONTEXT_SCHEMA",
    "AuthContext",
    "AuthContextNonceGuard",
    "AUTH_CONTEXT_DELEGATION_SCHEMA",
    "BranchService",
    "CampaignService",
    "CascadingOcrProvider",
    "CharacterStateUpdate",
    "ActorKnowledgeTransfer",
    "CharacterService",
    "ContinuityService",
    "ContinuityCommitService",
    "CONTENT_ACTOR_CARD_SCHEMA",
    "CONTENT_PACKAGE_FORMAT",
    "CONTENT_PACKAGE_SCHEMA_VERSION",
    "ContentPackageError",
    "Database",
    "DocumentQualityError",
    "DocumentLayoutProfile",
    "EmbeddingProfile",
    "EventService",
    "FACT_KEY_WRITE_ACTIONS",
    "GENERIC_DOCUMENT_LAYOUT_PROFILE",
    "MemoryService",
    "MANAGED_MODULE_SOURCE_FIELDS",
    "IdempotencyConflictError",
    "IdempotencyService",
    "IdempotencyWrite",
    "ImportJobError",
    "ImportJobService",
    "InitialActorGrant",
    "ModuleService",
    "NormalizedDocument",
    "OcrPageLayout",
    "OcrTextBlock",
    "PageLocator",
    "PdfDocumentConverter",
    "PdfTextLayoutProvider",
    "RapidOcrProvider",
    "RenderedDocumentPage",
    "apply_document_page_revisions",
    "RevisionService",
    "RuleProfileService",
    "RuleReceiptService",
    "RulePackService",
    "RuleService",
    "SnapshotService",
    "StateMutationService",
    "SubjectContextService",
    "SystemDefinition",
    "SystemRegistry",
    "VectorStore",
    "VectorFlushResult",
    "VectorIndexJobService",
    "configured_profiles",
    "build_content_actor_card",
    "build_content_package",
    "build_source_bundle",
    "blob_descriptor",
    "clean_source_evidence_text",
    "create_embedder",
    "default_local_principal",
    "dumps_content_archive",
    "extract_pdf_page_text",
    "file_sha256",
    "normalize_document",
    "normalized_document_page_text",
    "ocr_layout_text",
    "normalize_source_evidence_text",
    "loads_content_archive",
    "content_package_checksum",
    "request_hash",
    "render_pdf_page",
    "source_ref",
    "sign_auth_context",
    "sign_delegated_auth_context",
    "validate_content_package",
    "validate_content_actor_card",
    "validate_subject_context_fact",
    "verify_auth_context",
    "UnitOfWork",
    "IdempotencyIdentity",
]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value


__version__ = "0.3.0"
