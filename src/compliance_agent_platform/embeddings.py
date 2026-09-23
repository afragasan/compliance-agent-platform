"""Embedding-model factory.

Mirrors ``llm.py``'s pattern: by default every embedding call goes through Amazon
Bedrock (Titan Embeddings) so both the FAISS (local) and pgvector (deployed) vector
stores share one model and index/query vectors stay comparable across environments.

Setting ``CAP_FAKE_EMBEDDINGS`` swaps in a deterministic, dependency-free embedding
(a stable hash of the text projected into ``Settings.embedding_dimension``) — for
offline unit tests only, never production.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
from functools import lru_cache

from compliance_agent_platform.config import get_settings

logger = logging.getLogger(__name__)


def _deterministic_vector(text: str, dimension: int) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dimension)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _FakeEmbeddings:
    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    def embed_query(self, text: str) -> list[float]:
        return _deterministic_vector(text, self._dimension)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


def get_embedding_model():
    """Return the embedding model: a deterministic fake, or Bedrock Titan Embeddings."""
    if os.environ.get("CAP_FAKE_EMBEDDINGS"):
        logger.warning("CAP_FAKE_EMBEDDINGS active - no Bedrock call will be made")
        return _FakeEmbeddings(get_settings().embedding_dimension)
    return _bedrock_embeddings()


@lru_cache
def _bedrock_embeddings():
    from langchain_aws.embeddings import BedrockEmbeddings

    settings = get_settings()
    return BedrockEmbeddings(
        model_id=settings.bedrock_embedding_model_id, region_name=settings.aws_region
    )
