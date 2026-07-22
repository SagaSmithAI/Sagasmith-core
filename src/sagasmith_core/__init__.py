"""Shared runtime contracts for SagaSmith system packages."""

from sagasmith_core.access import AccessDeniedError, AccessService, default_local_principal
from sagasmith_core.branches import BranchService
from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.continuity import ContinuityService
from sagasmith_core.database import Database
from sagasmith_core.documents import (
    DOCUMENT_NORMALIZER_VERSION,
    DocumentQualityError,
    NormalizedDocument,
    PageLocator,
    PdfDocumentConverter,
    RapidOcrProvider,
    RenderedDocumentPage,
    file_sha256,
    normalize_document,
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
from sagasmith_core.idempotency import IdempotencyConflictError, IdempotencyService, request_hash
from sagasmith_core.import_jobs import ImportJobError, ImportJobService
from sagasmith_core.knowledge import ActorKnowledgeService
from sagasmith_core.memory import MemoryService
from sagasmith_core.modules import ModuleService
from sagasmith_core.revisions import RevisionService
from sagasmith_core.rule_packs import RulePackService
from sagasmith_core.rule_profiles import RuleProfileService
from sagasmith_core.rule_receipts import RuleReceiptService
from sagasmith_core.rules import RuleService
from sagasmith_core.snapshots import SnapshotService
from sagasmith_core.state import CharacterStateUpdate, StateMutationService
from sagasmith_core.systems import SystemDefinition, SystemRegistry
from sagasmith_core.vector import VectorStore

__all__ = [
    "DOCUMENT_NORMALIZER_VERSION",
    "BgeEmbedder",
    "BgeM3Embedder",
    "BgeSmallEnEmbedder",
    "BgeSmallZhEmbedder",
    "ActorKnowledgeService",
    "AccessDeniedError",
    "AccessService",
    "BranchService",
    "CampaignService",
    "CharacterStateUpdate",
    "CharacterService",
    "ContinuityService",
    "Database",
    "DocumentQualityError",
    "EmbeddingProfile",
    "EventService",
    "MemoryService",
    "IdempotencyConflictError",
    "IdempotencyService",
    "ImportJobError",
    "ImportJobService",
    "ModuleService",
    "NormalizedDocument",
    "PageLocator",
    "PdfDocumentConverter",
    "RapidOcrProvider",
    "RenderedDocumentPage",
    "RevisionService",
    "RuleProfileService",
    "RuleReceiptService",
    "RulePackService",
    "RuleService",
    "SnapshotService",
    "StateMutationService",
    "SystemDefinition",
    "SystemRegistry",
    "VectorStore",
    "configured_profiles",
    "create_embedder",
    "default_local_principal",
    "file_sha256",
    "normalize_document",
    "request_hash",
    "render_pdf_page",
]

__version__ = "0.2.0"
