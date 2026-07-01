"""RAG-aware tool — searches indexed codebase for relevant code context."""

import os
from langchain_core.tools import tool
from langchain_core.runnables.config import RunnableConfig

from app.auth.middleware import tenant_context
from app.rag.index.retriever import get_retriever_for_tenant


@tool
async def rag_search(query: str, top_k: int = 5, config: RunnableConfig | None = None) -> str:
    """Search the indexed codebase for relevant code. Only works if the current workspace has been indexed. Use file_list and file_read for unindexed projects."""
    tenant_id = tenant_context.get("default")
    retriever = await get_retriever_for_tenant(tenant_id)

    if tenant_id == "default" and not retriever.is_indexed:
        from app.rag.index.indexer import BASE_INDEX_DIR, get_index_dir_for_workspace
        from app.tools.agent_mode import get_workspace_root

        ws = get_workspace_root()
        ws_index = get_index_dir_for_workspace(ws)
        if not retriever.load_from_disk(str(ws_index)):
            if not retriever.load_from_disk(str(BASE_INDEX_DIR)):
                return (
                    "⚠️ No codebase indexed for this workspace. Use file_list and file_read "
                    "instead to explore the project. To index: python -m app.rag.index.indexer "
                    f"{ws}"
                )
    elif not retriever.is_indexed:
        return (
            "⚠️ No codebase indexed for this workspace. Use file_list and file_read "
            "instead to explore the project."
        )

    results = await retriever.search(query, top_k=top_k * 2)

    if not results:
        return f"🔍 No relevant code found for: '{query}'"

    reranker_type = os.environ.get("AGENT_RERANKER", "learned").lower()
    if reranker_type == "cross-encoder":
        from app.rag.reranking.cross_encoder import cross_encoder_rerank

        results = cross_encoder_rerank(query, results, top_k=top_k)
    else:
        from app.rag.reranking.learned import get_learned_reranker

        reranker = get_learned_reranker()
        results = reranker.rerank(query, results, top_k=top_k)

    output_parts = [f"🔍 Found {len(results)} relevant code chunk(s):\n"]

    for i, result in enumerate(results):
        file_name = result.file_path.split("/")[-1] if "/" in result.file_path else result.file_path
        content = result.content[:500] if len(result.content) > 500 else result.content
        output_parts.append(
            f"**{i + 1}. {file_name}:{result.start_line}-{result.end_line}** "
            f"({result.chunk_type} `{result.name}`)\n"
            f"```{result.language}\n{content}\n```\n"
        )

    return "\n".join(output_parts)
