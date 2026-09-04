"""Shared runtime contracts for SagaSmith system packages."""

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
from sagasmith_core.content_pack import (
    ACTOR_CARD_SCHEMA as CONTENT_ACTOR_CARD_SCHEMA,
)
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
from sagasmith_core.content_pack import (
    build_actor_card as build_content_actor_card,
)
from sagasmith_core.content_pack import (
    validate_actor_card as validate_content_actor_card,
)
from sagasmith_core.continuity import ContinuityService
from sagasmith_core.continuity_commit import (
    FACT_KEY_WRITE_ACTIONS,
    ContinuityCommitService,
)
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
]

__version__ = "0.2.3"
