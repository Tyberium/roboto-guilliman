"""Firestore vector retrieval and chat-history cache."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from roboto_guilliman.config import Settings, get_settings
from roboto_guilliman.embeddings import EmbeddingService
from roboto_guilliman.gcp_auth import optional_local_credentials
from roboto_guilliman.prompts import RetrievedChunk, build_cache_key

logger = logging.getLogger(__name__)


class RulesRetriever:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        credentials = optional_local_credentials()
        self.db = firestore.Client(
            project=self.settings.gcp_project_id,
            database=self.settings.firestore_database,
            credentials=credentials,
        )
        self.embedder = EmbeddingService(self.settings)

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        limit = top_k or self.settings.top_k
        query_vector = self.embedder.embed_query(query)
        collection = self.db.collection(self.settings.firestore_collection)

        results = (
            collection.find_nearest(
                vector_field="embedding",
                query_vector=Vector(query_vector),
                distance_measure=DistanceMeasure.COSINE,
                limit=limit,
            )
            .get()
        )

        chunks: list[RetrievedChunk] = []
        for doc in results:
            data = doc.to_dict() or {}
            section_hint = data.get("section_hint") or data.get("parent_section")
            figure_description = data.get("figure_description")
            chunks.append(
                RetrievedChunk(
                    text=str(data.get("text", "")),
                    page=data.get("page"),
                    section_hint=str(section_hint) if section_hint else None,
                    source=data.get("source"),
                    rule_number=data.get("rule_number"),
                    figure_description=str(figure_description) if figure_description else None,
                    distance=getattr(doc, "distance", None),
                )
            )
        return chunks


class ChatHistoryCache:
    """Serve repeat questions from Firestore without calling the LLM."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        credentials = optional_local_credentials()
        self.db = firestore.Client(
            project=self.settings.gcp_project_id,
            database=self.settings.firestore_database,
            credentials=credentials,
        )
        self.collection = self.db.collection(self.settings.chat_history_collection)

    @staticmethod
    def _doc_id(normalized_query: str) -> str:
        return hashlib.sha256(normalized_query.encode()).hexdigest()

    def get(self, query: str) -> str | None:
        doc_id = self._doc_id(build_cache_key(query))
        snapshot = self.collection.document(doc_id).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        answer = data.get("answer")
        return str(answer) if answer else None

    def put(self, query: str, answer: str) -> None:
        normalized = build_cache_key(query)
        self.collection.document(self._doc_id(normalized)).set(
            {
                "query": query.strip(),
                "normalized_query": normalized,
                "answer": answer,
                "updated_at": datetime.now(UTC),
            }
        )
        logger.info("Cached answer for query hash %s", self._doc_id(normalized)[:8])
