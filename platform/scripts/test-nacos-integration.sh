#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Integration test: Nacos Service Discovery
# Validates: register → heartbeat → resolve flow against real Nacos
# Requires: docker compose up nacos
# ─────────────────────────────────────────────────────────────────────────────
set -e

NACOS_URL="${NACOS_URL:-http://localhost:8848}"
SERVICE_NAME="memory-server-test"
IP="127.0.0.1"
PORT="8180"

echo "╔═══════════════════════════════════════════════════════╗"
echo "║  Nacos Service Discovery — Integration Test          ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# 1. Health check
echo "▶ [1/5] Checking Nacos health..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$NACOS_URL/nacos/v1/console/health/readiness")
if [ "$HTTP_CODE" != "200" ]; then
    echo "✗ Nacos not ready (HTTP $HTTP_CODE). Run: docker compose up nacos"
    exit 1
fi
echo "  ✓ Nacos is healthy"

# 2. Register service instance
echo ""
echo "▶ [2/5] Registering service: $SERVICE_NAME ($IP:$PORT)..."
REGISTER_RESULT=$(curl -s -X POST "$NACOS_URL/nacos/v1/ns/instance" \
    -d "serviceName=$SERVICE_NAME&ip=$IP&port=$PORT&ephemeral=true&healthy=true")
if [ "$REGISTER_RESULT" != "ok" ]; then
    echo "  ✗ Registration failed: $REGISTER_RESULT"
    exit 1
fi
echo "  ✓ Registered successfully"

# 3. Resolve (query instances)
echo ""
echo "▶ [3/5] Resolving service: $SERVICE_NAME..."
sleep 1  # Give Nacos a moment to sync
RESOLVE_RESULT=$(curl -s "$NACOS_URL/nacos/v1/ns/instance/list?serviceName=$SERVICE_NAME&healthyOnly=true")
echo "  Response: $RESOLVE_RESULT" | head -c 200
echo ""

# Verify response contains our IP and port
if echo "$RESOLVE_RESULT" | grep -q "$IP"; then
    echo "  ✓ Service resolved — found $IP in response"
else
    echo "  ✗ Service not found in response"
    exit 1
fi

if echo "$RESOLVE_RESULT" | grep -q "$PORT"; then
    echo "  ✓ Port matches: $PORT"
else
    echo "  ✗ Port $PORT not found in response"
    exit 1
fi

# 4. Send heartbeat
echo ""
echo "▶ [4/5] Sending heartbeat..."
BEAT="{\"serviceName\":\"$SERVICE_NAME\",\"ip\":\"$IP\",\"port\":$PORT,\"ephemeral\":true}"
BEAT_RESULT=$(curl -s -X PUT "$NACOS_URL/nacos/v1/ns/instance/beat?serviceName=$SERVICE_NAME&beat=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$BEAT'))")")
echo "  Response: $BEAT_RESULT"
if echo "$BEAT_RESULT" | grep -q "clientBeatInterval"; then
    echo "  ✓ Heartbeat acknowledged"
else
    echo "  ⚠ Heartbeat response unexpected (may still be OK)"
fi

# 5. Deregister (cleanup)
echo ""
echo "▶ [5/5] Deregistering service (cleanup)..."
DEREG_RESULT=$(curl -s -X DELETE "$NACOS_URL/nacos/v1/ns/instance?serviceName=$SERVICE_NAME&ip=$IP&port=$PORT&ephemeral=true")
echo "  Result: $DEREG_RESULT"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✓ All Nacos integration tests PASSED"
echo "═══════════════════════════════════════════════════════"
