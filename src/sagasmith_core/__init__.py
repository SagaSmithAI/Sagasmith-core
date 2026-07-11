"""Shared runtime contracts for SagaSmith system packages."""

from sagasmith_core.campaigns import CampaignService
from sagasmith_core.characters import CharacterService
from sagasmith_core.database import Database
from sagasmith_core.documents import (
    DocumentQualityError,
    NormalizedDocument,
    PdfDocumentConverter,
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
from sagasmith_core.memory import MemoryService
from sagasmith_core.modules import ModuleService
from sagasmith_core.revisions import RevisionService
from sagasmith_core.rule_profiles import RuleProfileService
from sagasmith_core.rules import RuleService
from sagasmith_core.snapshots import SnapshotService
from sagasmith_core.state import CharacterStateUpdate, StateMutationService
from sagasmith_core.systems import SystemDefinition, SystemRegistry
from sagasmith_core.vector import VectorStore

__all__ = [
    "BgeEmbedder",
    "BgeM3Embedder",
    "BgeSmallEnEmbedder",
    "BgeSmallZhEmbedder",
    "CampaignService",
    "CharacterStateUpdate",
    "CharacterService",
    "Database",
    "DocumentQualityError",
    "EmbeddingProfile",
    "EventService",
    "MemoryService",
    "ModuleService",
    "NormalizedDocument",
    "PdfDocumentConverter",
    "RevisionService",
    "RuleProfileService",
    "RuleService",
    "SnapshotService",
    "StateMutationService",
    "SystemDefinition",
    "SystemRegistry",
    "VectorStore",
    "configured_profiles",
    "create_embedder",
]

__version__ = "0.2.0"
