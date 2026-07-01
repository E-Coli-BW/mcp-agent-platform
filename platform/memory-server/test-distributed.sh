#!/bin/bash
# Distributed Memory Store — Integration Test Script
# Usage: ./test-distributed.sh
#
# Prerequisites: Docker + Docker Compose installed

set -e
cd "$(dirname "$0")"

echo "🚀 Starting distributed cluster..."
docker compose -f docker-compose-distributed.yml up -d --build

echo "⏳ Waiting for services to be healthy..."
sleep 15

echo ""
echo "=== 1. Health Check ==="
echo "Node 1:" && curl -sf http://localhost:8180/actuator/health | head -c 200 && echo
echo "Node 2:" && curl -sf http://localhost:8181/actuator/health | head -c 200 && echo

echo ""
echo "=== 2. Write to Node 1, Read from Node 2 (shared state) ==="
curl -sf -X POST http://localhost:8180/api/tools/call \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: test-tenant" \
  -d '{"tool":"memory_set","arguments":{"key":"dist-test","content":"hello from node 1"}}' && echo

sleep 1
echo "Read from Node 2:"
curl -sf -X POST http://localhost:8181/api/tools/call \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: test-tenant" \
  -d '{"tool":"memory_get","arguments":{"key":"dist-test"}}' && echo

echo ""
echo "=== 3. Node Failover Test ==="
echo "Stopping Node 1..."
docker compose -f docker-compose-distributed.yml stop memory-server-1
sleep 2
echo "Read from Node 2 (should still work):"
curl -sf -X POST http://localhost:8181/api/tools/call \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: test-tenant" \
  -d '{"tool":"memory_get","arguments":{"key":"dist-test"}}' && echo

echo ""
echo "=== 4. Redis Failover Test ==="
echo "Stopping Redis..."
docker compose -f docker-compose-distributed.yml stop redis
sleep 2
echo "Read from Node 2 (should fail fast — circuit breaker):"
curl -sf -X POST http://localhost:8181/api/tools/call \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: test-tenant" \
  -d '{"tool":"memory_get","arguments":{"key":"dist-test"}}' 2>&1 || echo "(Expected failure — circuit breaker open)"

echo ""
echo "Restarting Redis..."
docker compose -f docker-compose-distributed.yml start redis
sleep 3
echo "Read from Node 2 (should recover):"
curl -sf -X POST http://localhost:8181/api/tools/call \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: test-tenant" \
  -d '{"tool":"memory_get","arguments":{"key":"dist-test"}}' && echo

echo ""
echo "=== 5. Cleanup ==="
echo "Stopping all services..."
docker compose -f docker-compose-distributed.yml down

echo ""
echo "✅ Distributed test complete!"
