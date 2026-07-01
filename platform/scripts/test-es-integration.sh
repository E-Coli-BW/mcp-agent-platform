#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Integration test: Elasticsearch Memory Backend
# Validates: index → get → search → delete flow against real ES
# Requires: docker compose up elasticsearch
# ─────────────────────────────────────────────────────────────────────────────
set -e

ES_URL="${ELASTICSEARCH_URL:-http://localhost:9200}"
INDEX_NAME="memory-integration-test"

echo "╔═══════════════════════════════════════════════════════╗"
echo "║  Elasticsearch Memory Backend — Integration Test     ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# 1. Health check
echo "▶ [1/6] Checking Elasticsearch health..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$ES_URL/_cluster/health")
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ✗ ES not ready (HTTP $HTTP_CODE). Run: docker compose up elasticsearch"
    exit 1
fi
HEALTH=$(curl -s "$ES_URL/_cluster/health" | grep -o '"status":"[^"]*"')
echo "  ✓ ES is healthy ($HEALTH)"

# 2. Create index with proper mapping
echo ""
echo "▶ [2/6] Creating index: $INDEX_NAME..."
curl -s -X DELETE "$ES_URL/$INDEX_NAME" > /dev/null 2>&1 || true  # cleanup from previous run

CREATE_RESULT=$(curl -s -X PUT "$ES_URL/$INDEX_NAME" -H "Content-Type: application/json" -d '{
  "mappings": {
    "properties": {
      "key": { "type": "keyword" },
      "content": { "type": "text", "analyzer": "standard" },
      "indexed_at": { "type": "date" }
    }
  },
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  }
}')
if echo "$CREATE_RESULT" | grep -q '"acknowledged":true'; then
    echo "  ✓ Index created with proper mapping"
else
    echo "  ✗ Index creation failed: $CREATE_RESULT"
    exit 1
fi

# 3. Index documents
echo ""
echo "▶ [3/6] Indexing documents..."
curl -s -X PUT "$ES_URL/$INDEX_NAME/_doc/skill-thread-pool" -H "Content-Type: application/json" -d '{
  "key": "skill-thread-pool",
  "content": "12 isolated thread pools with CallerRunsPolicy for 200K vehicle connections",
  "indexed_at": "2026-06-10T10:00:00Z"
}' > /dev/null

curl -s -X PUT "$ES_URL/$INDEX_NAME/_doc/skill-redis-dedup" -H "Content-Type: application/json" -d '{
  "key": "skill-redis-dedup",
  "content": "Redis SETEX 48h TTL replacing DB-based deduplication for message idempotency",
  "indexed_at": "2026-06-10T10:01:00Z"
}' > /dev/null

curl -s -X PUT "$ES_URL/$INDEX_NAME/_doc/skill-circuit-breaker" -H "Content-Type: application/json" -d '{
  "key": "skill-circuit-breaker",
  "content": "Resilience4j circuit breaker with 50% threshold and 30s half-open window",
  "indexed_at": "2026-06-10T10:02:00Z"
}' > /dev/null

echo "  ✓ 3 documents indexed"

# Force refresh for immediate searchability
curl -s -X POST "$ES_URL/$INDEX_NAME/_refresh" > /dev/null

# 4. Get document by ID
echo ""
echo "▶ [4/6] Getting document by ID..."
GET_RESULT=$(curl -s "$ES_URL/$INDEX_NAME/_doc/skill-thread-pool")
if echo "$GET_RESULT" | grep -q "thread pools"; then
    echo "  ✓ Document retrieved: skill-thread-pool"
else
    echo "  ✗ Document not found"
    exit 1
fi

# 5. Full-text search
echo ""
echo "▶ [5/6] Full-text search: 'Redis dedup'..."
SEARCH_RESULT=$(curl -s -X POST "$ES_URL/$INDEX_NAME/_search" -H "Content-Type: application/json" -d '{
  "query": {
    "multi_match": {
      "query": "Redis dedup",
      "fields": ["content", "key"],
      "fuzziness": "AUTO"
    }
  }
}')
HIT_COUNT=$(echo "$SEARCH_RESULT" | grep -o '"total":{"value":[0-9]*' | grep -o '[0-9]*$')
if [ "$HIT_COUNT" -ge 1 ]; then
    echo "  ✓ Search returned $HIT_COUNT result(s)"
else
    echo "  ✗ Search returned 0 results"
    exit 1
fi

# 6. Delete document
echo ""
echo "▶ [6/6] Deleting document..."
DEL_RESULT=$(curl -s -X DELETE "$ES_URL/$INDEX_NAME/_doc/skill-thread-pool")
if echo "$DEL_RESULT" | grep -q '"result":"deleted"'; then
    echo "  ✓ Document deleted successfully"
else
    echo "  ✗ Delete failed: $DEL_RESULT"
    exit 1
fi

# Cleanup
curl -s -X DELETE "$ES_URL/$INDEX_NAME" > /dev/null

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✓ All Elasticsearch integration tests PASSED"
echo "═══════════════════════════════════════════════════════"
