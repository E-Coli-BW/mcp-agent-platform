# Case analysis: where chunking strategies disagreed

The examples below compare target-line coverage for each query. They show retrieval outcomes, not answer-generation correctness. Chunk ranks are from the same hybrid retriever configuration.

## Largest gains over controlled AST
### rag-02 · 100 lines / 5 overlap / file label vs AST (+92.4 pp coverage)
- Query: How does the code indexer avoid splitting a method in the middle, and which syntax units does it keep?
- Gold: `platform/agent-server/app/rag/chunking/code.py` · `chunk_file` · lines 135–200
- AST: coverage@5 0.0%; first overlap rank None; top-3: #1 `AstCompressor.java:36-57` (AstCompressor.java); #2 `code.py:82-103` (code.py); #3 `RagIndexer.java:14-43` (RagIndexer.java)
- 100 lines / 5 overlap / file label: coverage@5 92.4%; first overlap rank 1; top-3: #1 `code.py:96-195` (code.py); #2 `code.py:1-100` (code.py); #3 `CodeChunk.java:1-23` (CodeChunk.java)
### workspace-02 · 100 lines / 5 overlap / file label vs AST (+45.3 pp coverage)
- Query: How does code search invoke ripgrep and turn its matches into bounded results?
- Gold: `platform/agent-server/app/tools/definitions.py` · `file_search` · lines 150–224
- AST: coverage@5 0.0%; first overlap rank None; top-3: #1 `test_file_tools.py:160-164` (test_file_tools.py); #2 `rag_tool.py:21-93` (rag_tool.py); #3 `AstCompressor.java:59-64` (AstCompressor.java)
- 100 lines / 5 overlap / file label: coverage@5 45.3%; first overlap rank 3; top-3: #1 `SearchResult.java:1-11` (SearchResult.java); #2 `customer_qa_reranker_benchmark.py:1-82` (customer_qa_reranker_benchmark.py); #3 `definitions.py:191-290` (definitions.py)
### runtime-01 · 50 lines / 10 overlap / file label vs AST (+10.0 pp coverage)
- Query: Where does one chat turn hold its session lane while streaming model and tool events?
- Gold: `platform/agent-server/app/runtime/runner.py` · `stream_turn` · lines 127–567
- AST: coverage@5 0.0%; first overlap rank None; top-3: #1 `chat_stream.py:27-73` (chat_stream.py); #2 `test_chat_protocol.py:192-198` (test_chat_protocol.py); #3 `test_runtime_runner.py:102-125` (test_runtime_runner.py)
- 50 lines / 10 overlap / file label: coverage@5 10.0%; first overlap rank 1; top-3: #1 `runner.py:121-170` (runner.py); #2 `progress_bus.py:1-50` (progress_bus.py); #3 `AgentService.java:81-120` (AgentService.java)

## Largest losses against controlled AST
### rag-14 · 50 lines / 5 overlap / file label vs AST (-100.0 pp coverage)
- Query: Where are retrieval candidates compared with expected source files to calculate Recall and MRR?
- Gold: `platform/agent-server/app/rag/eval/ranking.py` · `evaluate_retriever` · lines 44–69
- AST: coverage@5 100.0%; first overlap rank 1; top-3: #1 `ranking.py:44-69` (ranking.py); #2 `ranking.py:118-140` (ranking.py); #3 `retrieval_pipeline.py:68-86` (retrieval_pipeline.py)
- 50 lines / 5 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `customer_qa.py:1-33` (customer_qa.py); #2 `ranking.py:91-140` (ranking.py); #3 `ranking.py:136-156` (ranking.py)
### rag-16 · 50 lines / 0 overlap / file label vs AST (-100.0 pp coverage)
- Query: In the Java implementation, where does Tree-sitter walk source nodes and emit code chunks?
- Gold: `platform/agent-server-java/src/main/java/com/example/agent/rag/TreeSitterChunker.java` · `chunkFile` · lines 51–83
- AST: coverage@5 100.0%; first overlap rank 4; top-3: #1 `code.py:135-200` (code.py); #2 `code.py:156-182` (code.py); #3 `ast.py:92-196` (ast.py)
- 50 lines / 0 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `code.py:1-50` (code.py); #2 `code.py:151-200` (code.py); #3 `code.py:51-100` (code.py)
### rag-19 · 50 lines / 5 overlap / file label vs AST (-100.0 pp coverage)
- Query: Where are Java vector and BM25 rankings fused into one ordered list?
- Gold: `platform/agent-server-java/src/main/java/com/example/agent/rag/HybridRetriever.java` · `rrfMerge` · lines 90–106
- AST: coverage@5 100.0%; first overlap rank 4; top-3: #1 `llm.py:86-137` (llm.py); #2 `test_llm_reranker.py:33-64` (test_llm_reranker.py); #3 `test_llm_reranker.py:41-43` (test_llm_reranker.py)
- 50 lines / 5 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `llm.py:46-95` (llm.py); #2 `llm.py:136-185` (llm.py); #3 `llm.py:1-50` (llm.py)
### rag-20 · 50 lines / 0 overlap / file label vs AST (-100.0 pp coverage)
- Query: Where does the Java Ollama embedder truncate long text before sending the request?
- Gold: `platform/agent-server-java/src/main/java/com/example/agent/rag/OllamaEmbedder.java` · `truncate` · lines 99–101
- AST: coverage@5 100.0%; first overlap rank 4; top-3: #1 `embedder.py:70-85` (embedder.py); #2 `embedder.py:88-136` (embedder.py); #3 `__init__.py:1-14` (__init__.py)
- 50 lines / 0 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `embedder.py:51-100` (embedder.py); #2 `embedder.py:101-150` (embedder.py); #3 `embedder.py:1-50` (embedder.py)
### rag-21 · 100 lines / 5 overlap / file label vs AST (-100.0 pp coverage)
- Query: How does the Java indexer rebuild the workspace index from the current source tree?
- Gold: `platform/agent-server-java/src/main/java/com/example/agent/rag/RagIndexer.java` · `indexWorkspace` · lines 32–42
- AST: coverage@5 100.0%; first overlap rank 1; top-3: #1 `RagIndexer.java:14-43` (RagIndexer.java); #2 `__init__.py:1-33` (__init__.py); #3 `RagIndexer.java:32-42` (RagIndexer.java)
- 100 lines / 5 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `RagSearchTool.java:1-92` (RagSearchTool.java); #2 `indexer.py:1-100` (indexer.py); #3 `rag_tool.py:1-93` (rag_tool.py)
### rag-22 · 50 lines / 0 overlap / file label vs AST (-100.0 pp coverage)
- Query: Before a RAG search, how does the Java tool ensure the active workspace has an index?
- Gold: `platform/agent-server-java/src/main/java/com/example/agent/tools/RagSearchTool.java` · `ensureIndex` · lines 72–88
- AST: coverage@5 100.0%; first overlap rank 3; top-3: #1 `rag_tool.py:21-93` (rag_tool.py); #2 `indexer.py:107-142` (indexer.py); #3 `RagSearchTool.java:72-88` (RagSearchTool.java)
- 50 lines / 0 overlap / file label: coverage@5 0.0%; first overlap rank None; top-3: #1 `rag_tool.py:1-50` (rag_tool.py); #2 `RagIndexer.java:1-43` (RagIndexer.java); #3 `indexer.py:1-50` (indexer.py)
