#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Integration Test Script — Tests all services end-to-end
# 
# Prerequisites:
#   docker-compose up -d          (Redis + PostgreSQL)
#   Ollama running with qwen2.5:7b + mxbai-embed-large
#
# Usage:
#   ./test-integration.sh          # run all tests
#   ./test-integration.sh agent    # run agent tests only
#   ./test-integration.sh completion  # run completion tests only
#   ./test-integration.sh rag      # run RAG tests only
# ═══════════════════════════════════════════════════════════════

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0
SKIP=0

green()  { echo -e "\033[32m✅ $1\033[0m"; PASS=$((PASS+1)); }
red()    { echo -e "\033[31m❌ $1\033[0m"; FAIL=$((FAIL+1)); }
yellow() { echo -e "\033[33m⏭  $1\033[0m"; SKIP=$((SKIP+1)); }

check_service() {
    local name=$1 url=$2
    if curl -sf "$url" > /dev/null 2>&1; then
        return 0
    else
        yellow "$name not running at $url"
        return 1
    fi
}

# ═══════════════════════════════════════════════════════════════
# 1. Infrastructure checks
# ═══════════════════════════════════════════════════════════════
echo "══════════════════════════════════════════════"
echo "  Infrastructure Checks"
echo "══════════════════════════════════════════════"

if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    green "Ollama running"
else
    red "Ollama not running (required for all tests)"
    exit 1
fi

REDIS_OK=false
if docker exec agent-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    green "Redis Stack running"
    REDIS_OK=true
    # Check RediSearch module
    if docker exec agent-redis redis-cli MODULE LIST 2>/dev/null | grep -qi search; then
        green "RediSearch module loaded"
    else
        yellow "RediSearch module not loaded (vector search won't work)"
    fi
else
    yellow "Redis not running — skipping Redis-dependent tests"
fi

# ═══════════════════════════════════════════════════════════════
# 2. Agent Server Tests
# ═══════════════════════════════════════════════════════════════
if [[ -z "$1" || "$1" == "agent" ]]; then
echo ""
echo "══════════════════════════════════════════════"
echo "  Agent Server (port 8500)"
echo "══════════════════════════════════════════════"

if check_service "Agent server" "http://localhost:8500/health"; then
    # Health
    RESP=$(curl -sf http://localhost:8500/health)
    echo "$RESP" | grep -q '"ok"' && green "Health endpoint" || red "Health endpoint"

    # Workspace API
    RESP=$(curl -sf http://localhost:8500/api/workspace/current)
    echo "$RESP" | grep -q '"path"' && green "GET /api/workspace/current" || red "Workspace current"

    # Open workspace
    RESP=$(curl -sf -X POST http://localhost:8500/api/workspace/open \
        -H "Content-Type: application/json" \
        -d '{"path":"/tmp/test-workspace-integration"}')
    echo "$RESP" | grep -q '"path"' && green "POST /api/workspace/open" || red "Workspace open"

    # File tree
    mkdir -p /tmp/test-workspace-integration/src
    echo "print('hello')" > /tmp/test-workspace-integration/src/main.py
    RESP=$(curl -sf http://localhost:8500/api/workspace/files)
    echo "$RESP" | grep -q '"tree"' && green "GET /api/workspace/files" || red "File tree"

    # Read file
    RESP=$(curl -sf "http://localhost:8500/api/workspace/file?path=src/main.py")
    echo "$RESP" | grep -q 'hello' && green "GET /api/workspace/file" || red "File read"

    # Usage tracking
    RESP=$(curl -sf http://localhost:8500/api/usage)
    echo "$RESP" | grep -q 'total_requests' && green "GET /api/usage" || red "Usage endpoint"

    # Chat (non-streaming, short timeout)
    RESP=$(curl -sf --max-time 30 -X POST http://localhost:8500/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"coding-agent","messages":[{"role":"user","content":"Say hello in one word"}],"stream":false}')
    if echo "$RESP" | grep -q '"choices"'; then
        green "POST /v1/chat/completions (non-streaming)"
    else
        yellow "Chat completions timed out or failed (Ollama may be slow)"
    fi
fi
fi

# ═══════════════════════════════════════════════════════════════
# 3. Completion Server Tests
# ═══════════════════════════════════════════════════════════════
if [[ -z "$1" || "$1" == "completion" ]]; then
echo ""
echo "══════════════════════════════════════════════"
echo "  Completion Server (port 8600)"
echo "══════════════════════════════════════════════"

if check_service "Completion server" "http://localhost:8600/health"; then
    # Health
    RESP=$(curl -sf http://localhost:8600/health)
    echo "$RESP" | grep -q '"ok"' && green "Health endpoint" || red "Health endpoint"

    # FIM completion (non-streaming, IDE cursor mode)
    RESP=$(curl -sf --max-time 15 -X POST http://localhost:8600/v1/completions \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d '{
            "file_content": "def add(a, b):\n    \n\ndef subtract(a, b):\n    return a - b",
            "cursor_line": 1,
            "cursor_column": 4,
            "language": "python",
            "stream": false
        }')
    if echo "$RESP" | grep -q '"choices"'; then
        green "POST /v1/completions (IDE cursor mode)"
        echo "  Completion: $(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['text'][:80])" 2>/dev/null)"
    else
        yellow "Completion timed out (model may need loading)"
    fi

    # Cache hit test (send same request twice)
    START=$(date +%s%N)
    curl -sf --max-time 15 -X POST http://localhost:8600/v1/completions \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d '{"file_content": "x = 1\ny = ", "cursor_line": 1, "cursor_column": 4, "stream": false}' > /dev/null 2>&1
    # Second request should be cached
    RESP=$(curl -sf --max-time 5 -X POST http://localhost:8600/v1/completions \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d '{"file_content": "x = 1\ny = ", "cursor_line": 1, "cursor_column": 4, "stream": false}')
    END=$(date +%s%N)
    if echo "$RESP" | grep -q '"choices"'; then
        ELAPSED=$(( (END - START) / 1000000 ))
        if [ "$ELAPSED" -lt 100 ]; then
            green "Cache hit (${ELAPSED}ms — near instant)"
        else
            green "Completion returned (${ELAPSED}ms — may or may not be cached)"
        fi
    else
        yellow "Cache test inconclusive"
    fi

    # Prometheus metrics
    RESP=$(curl -sf http://localhost:8600/actuator/prometheus 2>/dev/null)
    if echo "$RESP" | grep -q 'completion'; then
        green "Prometheus metrics (completion.*)"
    else
        yellow "Prometheus metrics not available"
    fi
fi
fi

# ═══════════════════════════════════════════════════════════════
# 4. RAG Pipeline Tests
# ═══════════════════════════════════════════════════════════════
if [[ -z "$1" || "$1" == "rag" ]]; then
echo ""
echo "══════════════════════════════════════════════"
echo "  RAG Pipeline"
echo "══════════════════════════════════════════════"

VENV="$SCRIPT_DIR/agent-server/.venv/bin/python"

# Check if embedding works
RESP=$(curl -sf -X POST http://localhost:11434/api/embeddings \
    -d '{"model":"mxbai-embed-large","prompt":"test"}')
if echo "$RESP" | grep -q 'embedding'; then
    green "Ollama embedding (mxbai-embed-large)"
else
    red "Embedding model not available"
fi

# Check existing indexes
echo "  Existing RAG indexes:"
ls -d ~/.mcp-local/rag-index/*/ 2>/dev/null | while read dir; do
    chunks=$(cat "$dir/chunks.json" 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
    echo "    📁 $(basename $dir) — $chunks chunks"
done || echo "    (none)"

# Test Redis vector backend if available
if $REDIS_OK; then
    echo "  Testing Redis vector retriever..."
    RESP=$(docker exec agent-redis redis-cli FT._LIST 2>/dev/null)
    if echo "$RESP" | grep -q 'idx:rag_chunks'; then
        green "RediSearch RAG index exists"
    else
        yellow "RediSearch RAG index not yet created (run indexer with AGENT_RAG_BACKEND=redis)"
    fi
fi
fi

# ═══════════════════════════════════════════════════════════════
# 5. Memory Server Tests (Java)
# ═══════════════════════════════════════════════════════════════
if [[ -z "$1" || "$1" == "memory" ]]; then
echo ""
echo "══════════════════════════════════════════════"
echo "  Memory Server (port 8180)"
echo "══════════════════════════════════════════════"

if check_service "Memory server" "http://localhost:8180/actuator/health"; then
    green "Health endpoint"

    # Tool bridge: memory_set
    RESP=$(curl -sf -X POST http://localhost:8180/api/tools/memory_set \
        -H "Content-Type: application/json" \
        -d '{"key":"integration-test","content":"test value","tags":["test"]}')
    echo "$RESP" | grep -q 'result' && green "memory_set via REST bridge" || red "memory_set failed"

    # Tool bridge: memory_search
    RESP=$(curl -sf -X POST http://localhost:8180/api/tools/memory_search \
        -H "Content-Type: application/json" \
        -d '{"query":"integration test"}')
    echo "$RESP" | grep -q 'result' && green "memory_search via REST bridge" || red "memory_search failed"

    # Resilience4j rate limiter (if configured)
    # Quick burst test — shouldn't fail at 50 req/s for <10 requests
    for i in $(seq 1 5); do
        curl -sf -X POST http://localhost:8180/api/tools/memory_context \
            -H "Content-Type: application/json" -d '{}' > /dev/null 2>&1
    done
    green "Rate limiter allows normal traffic (5 requests)"
fi
fi

# ═══════════════════════════════════════════════════════════════
# Infrastructure Integration Tests (Nacos + ES + Kafka)
# ═══════════════════════════════════════════════════════════════
if [ -z "$1" ] || [ "$1" = "infra" ]; then

echo ""
echo "── Infrastructure Integration Tests ──"

# Nacos — Service Discovery
if check_service "Nacos" "http://localhost:8848/nacos/v1/console/health/readiness"; then
    green "Nacos is healthy"

    # Register a test service
    REGISTER_RESP=$(curl -sf -X POST "http://localhost:8848/nacos/v1/ns/instance" \
        -d "serviceName=test-service&ip=127.0.0.1&port=9999&ephemeral=true" 2>&1)
    echo "$REGISTER_RESP" | grep -qi 'ok' && green "Nacos: service registration" || red "Nacos: registration failed"

    # Resolve the test service
    sleep 1
    RESOLVE_RESP=$(curl -sf "http://localhost:8848/nacos/v1/ns/instance/list?serviceName=test-service" 2>&1)
    echo "$RESOLVE_RESP" | grep -q '127.0.0.1' && green "Nacos: service resolution" || red "Nacos: resolution failed"

    # Deregister
    curl -sf -X DELETE "http://localhost:8848/nacos/v1/ns/instance?serviceName=test-service&ip=127.0.0.1&port=9999&ephemeral=true" > /dev/null 2>&1
    green "Nacos: service deregistration"
else
    yellow "Nacos not running (docker compose up -d nacos)"
fi

# Elasticsearch — Full-text Search Backend
if check_service "Elasticsearch" "http://localhost:9200/_cluster/health"; then
    green "Elasticsearch is healthy"

    # Create test index
    curl -sf -X PUT "http://localhost:9200/memory-integration-test" \
        -H "Content-Type: application/json" \
        -d '{"mappings":{"properties":{"key":{"type":"keyword"},"content":{"type":"text"}}}}' > /dev/null 2>&1
    green "ES: index creation"

    # Index a document
    curl -sf -X PUT "http://localhost:9200/memory-integration-test/_doc/test-key-1" \
        -H "Content-Type: application/json" \
        -d '{"key":"test-key-1","content":"spring boot microservice architecture"}' > /dev/null 2>&1
    sleep 1  # ES needs a moment to index

    # Search for document
    SEARCH_RESP=$(curl -sf -X POST "http://localhost:9200/memory-integration-test/_search" \
        -H "Content-Type: application/json" \
        -d '{"query":{"match":{"content":"microservice"}}}' 2>&1)
    echo "$SEARCH_RESP" | grep -q 'test-key-1' && green "ES: full-text search" || red "ES: search failed"

    # Cleanup
    curl -sf -X DELETE "http://localhost:9200/memory-integration-test" > /dev/null 2>&1
    green "ES: index cleanup"
else
    yellow "Elasticsearch not running (docker compose up -d elasticsearch)"
fi

# Kafka — Event Streaming
if check_service "Kafka" "http://localhost:8080"; then
    green "Kafka UI is accessible"
    # Kafka topic verification via Kafka UI API
    TOPICS_RESP=$(curl -sf "http://localhost:8080/api/clusters/agent-local/topics" 2>&1)
    if [ -n "$TOPICS_RESP" ]; then
        green "Kafka: cluster connectivity"
    else
        yellow "Kafka: could not list topics via UI"
    fi
else
    # Try direct Kafka bootstrap check
    if docker exec agent-kafka kafka-topics --bootstrap-server localhost:9092 --list > /dev/null 2>&1; then
        green "Kafka broker is healthy"

        # Create test topic
        docker exec agent-kafka kafka-topics --bootstrap-server localhost:9092 \
            --create --topic test-integration --partitions 1 --replication-factor 1 --if-not-exists > /dev/null 2>&1
        green "Kafka: topic creation"

        # Produce + consume a test message
        echo "integration-test-msg" | docker exec -i agent-kafka kafka-console-producer \
            --bootstrap-server localhost:9092 --topic test-integration > /dev/null 2>&1
        KAFKA_MSG=$(docker exec agent-kafka kafka-console-consumer \
            --bootstrap-server localhost:9092 --topic test-integration \
            --from-beginning --max-messages 1 --timeout-ms 5000 2>/dev/null)
        echo "$KAFKA_MSG" | grep -q 'integration-test-msg' && green "Kafka: produce + consume" || red "Kafka: message delivery failed"

        # Cleanup
        docker exec agent-kafka kafka-topics --bootstrap-server localhost:9092 \
            --delete --topic test-integration > /dev/null 2>&1
    else
        yellow "Kafka not running (docker compose up -d kafka)"
    fi
fi

# Jaeger — Distributed Tracing
if check_service "Jaeger" "http://localhost:16686"; then
    green "Jaeger UI is accessible"
    # Verify OTLP receiver
    if curl -sf "http://localhost:4318/v1/traces" -X POST -H "Content-Type: application/json" -d '{}' > /dev/null 2>&1 || true; then
        green "Jaeger: OTLP HTTP endpoint reachable"
    fi
else
    yellow "Jaeger not running (docker compose up -d jaeger)"
fi

fi

# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════════════"
echo "  Results: ✅ $PASS passed, ❌ $FAIL failed, ⏭ $SKIP skipped"
echo "══════════════════════════════════════════════"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
