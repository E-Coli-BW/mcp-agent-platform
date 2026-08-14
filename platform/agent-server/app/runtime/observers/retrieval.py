"""Retrieval trace and learned-reranker feedback observer."""

from __future__ import annotations

import os
from typing import Any

from app.context.request_context import get_request_context
from app.runtime.observers.base import (
    RuntimeObservation,
    TurnObservationReport,
    TurnObserverContext,
)


class RetrievalFeedbackObserver:
    def __init__(
        self,
        context: TurnObserverContext,
        report: TurnObservationReport,
    ) -> None:
        self._context = context
        self._report = report
        self._last_query: str | None = None
        self._last_chunks: list[Any] = []
        self._file_reads: list[str] = []
        self._tools_since_rag = 0

    async def observe(self, observation: RuntimeObservation) -> None:
        if observation.kind == "tool_started":
            self._track_tool_start(observation.payload)
        elif observation.kind == "tool_completed":
            await self._record_trace(observation.payload)
        elif observation.kind == "turn_finished":
            self._record_feedback()

    def _track_tool_start(self, payload: dict[str, Any]) -> None:
        tool_name = str(payload["tool"])
        tool_input = dict(payload["input"])
        if tool_name == "rag_search":
            self._last_query = str(tool_input.get("query", ""))
            self._last_chunks = []
            self._file_reads = []
            self._tools_since_rag = 0
        elif (
            tool_name == "file_read"
            and self._last_query
            and self._tools_since_rag <= 3
        ):
            self._file_reads.append(str(tool_input.get("path", "")))
        if self._last_query:
            self._tools_since_rag += 1

    async def _record_trace(self, payload: dict[str, Any]) -> None:
        if payload["tool"] != "rag_search" or not self._last_query:
            return
        from app.feedback.store import record_retrieval_trace
        from app.rag.index.retriever import get_retriever_for_tenant

        collection = get_request_context().knowledge_collection or None
        retriever = await get_retriever_for_tenant(
            self._context.tenant_id,
            collection=collection,
        )
        self._last_chunks = await retriever.search(self._last_query, top_k=5)
        trace = record_retrieval_trace(
            run_id=self._context.run_id,
            session_id=self._context.session_id,
            tenant_id=self._context.tenant_id,
            agent=self._context.model,
            query=self._last_query,
            chunks=[
                {
                    "path": chunk.file_path,
                    "name": chunk.name,
                    "score": chunk.score,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                }
                for chunk in self._last_chunks
            ],
            embedding_version=os.environ.get(
                "AGENT_EMBEDDING_MODEL",
                "mxbai-embed-large",
            ),
            reranker_version=os.environ.get("AGENT_RERANKER", "learned"),
            collection=collection or "default",
        )
        self._report.retrieval_trace_ids.append(trace["trace_id"])

    def _record_feedback(self) -> None:
        if not self._last_query or not self._file_reads:
            return
        from app.rag.reranking.learned import get_learned_reranker

        reranker = get_learned_reranker()
        reranker.log_feedback(
            self._last_query,
            self._last_chunks,
            self._file_reads,
        )
        info = reranker.get_weights_info()
        if info["feedback_count"] > 0 and info["feedback_count"] % 50 == 0:
            reranker.retrain()