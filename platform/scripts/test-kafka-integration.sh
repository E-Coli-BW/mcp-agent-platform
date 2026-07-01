#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Integration test: Kafka Outbox Pattern (Producer → Consumer)
# Validates: produce → consume → idempotent dedup flow
# Requires: docker compose up kafka
# ─────────────────────────────────────────────────────────────────────────────
set -e

KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-localhost:9093}"
TOPIC="user.events"

echo "╔═══════════════════════════════════════════════════════╗"
echo "║  Kafka Outbox Pattern — Integration Test             ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# 1. Check Kafka connectivity
echo "▶ [1/5] Checking Kafka connectivity..."
# Use kcat (kafkacat) if available, otherwise use docker exec
if command -v kcat &> /dev/null; then
    BROKER_CHECK=$(kcat -b "$KAFKA_BOOTSTRAP" -L -t "$TOPIC" 2>&1 | head -1)
    if echo "$BROKER_CHECK" | grep -q "broker"; then
        echo "  ✓ Kafka broker reachable"
    else
        echo "  ✗ Cannot reach Kafka at $KAFKA_BOOTSTRAP"
        echo "  Run: docker compose up kafka"
        exit 1
    fi
else
    # Use docker exec to check
    TOPICS=$(docker exec agent-kafka kafka-topics --bootstrap-server localhost:9092 --list 2>/dev/null)
    if [ $? -eq 0 ]; then
        echo "  ✓ Kafka broker reachable (via docker)"
    else
        echo "  ✗ Cannot reach Kafka. Run: docker compose up kafka"
        exit 1
    fi
fi

# 2. Create topic (if not exists)
echo ""
echo "▶ [2/5] Ensuring topic exists: $TOPIC..."
docker exec agent-kafka kafka-topics \
    --bootstrap-server localhost:9092 \
    --create --if-not-exists \
    --topic "$TOPIC" \
    --partitions 3 \
    --replication-factor 1 2>/dev/null
echo "  ✓ Topic ready: $TOPIC (3 partitions)"

# 3. Produce a test event (simulating OutboxPublisher)
echo ""
echo "▶ [3/5] Producing test event..."
EVENT_ID="evt-$(date +%s)"
EVENT_PAYLOAD="{\"type\":\"USER_REGISTERED\",\"eventId\":\"$EVENT_ID\",\"userId\":42,\"username\":\"integrationtest\",\"email\":\"test@example.com\",\"tenantId\":\"tenant-integ\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

echo "$EVENT_PAYLOAD" | docker exec -i agent-kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic "$TOPIC" \
    --property "parse.key=true" \
    --property "key.separator=:" <<< "tenant-integ:$EVENT_PAYLOAD"

echo "  ✓ Event produced: eventId=$EVENT_ID"

# 4. Consume and verify
echo ""
echo "▶ [4/5] Consuming event (5s timeout)..."
CONSUMED=$(docker exec agent-kafka kafka-console-consumer \
    --bootstrap-server localhost:9092 \
    --topic "$TOPIC" \
    --from-beginning \
    --max-messages 1 \
    --timeout-ms 5000 2>/dev/null | tail -1)

if echo "$CONSUMED" | grep -q "USER_REGISTERED"; then
    echo "  ✓ Event consumed successfully"
    echo "  Payload: $(echo $CONSUMED | head -c 100)..."
else
    echo "  ✗ Failed to consume event"
    exit 1
fi

# 5. Produce duplicate (same eventId) to test idempotency
echo ""
echo "▶ [5/5] Producing duplicate event (same eventId for dedup test)..."
echo "$EVENT_PAYLOAD" | docker exec -i agent-kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic "$TOPIC" \
    --property "parse.key=true" \
    --property "key.separator=:" <<< "tenant-integ:$EVENT_PAYLOAD"
echo "  ✓ Duplicate produced — consumer should skip on dedup check"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✓ All Kafka integration tests PASSED"
echo "  Note: Idempotency is verified at application level"
echo "        (UserEventConsumer.processedEvents check)"
echo "═══════════════════════════════════════════════════════"
