#!/bin/bash
# run-java-vs-python.sh — Quick-start script for the Java vs Python experiment.
#
# Prerequisites:
#   - Ollama running with qwen2.5:7b pulled
#   - Redis running (redis://localhost:6379)
#   - memory-server running (port 8180)
#   - codeexec-server running (port 8380)
#
# This script starts BOTH agent servers and runs the experiment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_PORT=8500
JAVA_PORT=8580
RUNS=${1:-3}  # Default 3 repetitions (quick run). Use 5+ for proper stats.

echo "═══════════════════════════════════════════════════════════════"
echo "  Java vs Python Agent Server — Head-to-Head Experiment"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Python server: http://localhost:${PYTHON_PORT}"
echo "  Java server:   http://localhost:${JAVA_PORT}"
echo "  Repetitions:   ${RUNS}"
echo ""

# ── Helper functions ─────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    [ -n "${PYTHON_PID:-}" ] && kill "$PYTHON_PID" 2>/dev/null || true
    [ -n "${JAVA_PID:-}" ] && kill "$JAVA_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT

wait_for_health() {
    local url=$1
    local name=$2
    local max_attempts=30
    local attempt=0
    while [ $attempt -lt $max_attempts ]; do
        if curl -sf "$url/health" > /dev/null 2>&1; then
            echo "  ✅ $name is healthy"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    echo "  ❌ $name failed to start after ${max_attempts}s"
    return 1
}

# ── Check prerequisites ──────────────────────────────────────────────
echo "📋 Checking prerequisites..."

if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "  ❌ Ollama not running at localhost:11434"
    exit 1
fi
echo "  ✅ Ollama running"

# ── Workspace setup ───────────────────────────────────────────────────
# Eval cases reference real project files (README.md, platform/agent-server/...).
# Point both servers at the repo root so file tools can find them.
EVAL_WORKSPACE="$REPO_ROOT"
echo "📁 Eval workspace: ${EVAL_WORKSPACE}"

# ── Start Python agent server ────────────────────────────────────────
echo ""
echo "🐍 Starting Python agent server on port ${PYTHON_PORT}..."
cd "$REPO_ROOT/platform/agent-server"

AGENT_PORT=$PYTHON_PORT \
AGENT_DEFAULT_MODEL=qwen2.5:7b \
AGENT_AGENT_GRAPH_VERSION=v2 \
AGENT_REFLEXION_ENABLED=false \
AGENT_DIRECT_TOOL_ROUTING_ENABLED=false \
AGENT_SUBAGENT_VERIFIER_ENABLED=false \
AGENT_WORKSPACE="$EVAL_WORKSPACE" \
AGENT_MULTI_TENANT_WORKSPACE=false \
.venv/bin/uvicorn app.main:app --port $PYTHON_PORT --log-level warning &
PYTHON_PID=$!

# ── Start Java agent server ──────────────────────────────────────────
echo "☕ Starting Java agent server on port ${JAVA_PORT}..."
cd "$REPO_ROOT/platform/agent-server-java"

SERVER_PORT=$JAVA_PORT \
AGENT_DEFAULT_MODEL=qwen2.5:7b \
command mvn spring-boot:run \
  -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo \
  -Dspring-boot.run.arguments="--server.port=${JAVA_PORT} --agent.workspace=${EVAL_WORKSPACE} --agent.multi-tenant-workspace=false" \
  -q &
JAVA_PID=$!

# ── Wait for both servers ────────────────────────────────────────────
echo ""
echo "⏳ Waiting for servers to start..."
wait_for_health "http://localhost:${PYTHON_PORT}" "Python server"
wait_for_health "http://localhost:${JAVA_PORT}" "Java server"

# ── Pre-warm LLM (one throwaway request to each) ─────────────────────
echo ""
echo "🔥 Pre-warming LLM..."
# Generate a simple JWT for the warmup requests
TOKEN=$(python3 -c "
import jwt, time
payload = {'sub':'warmup','tenant_id':'eval','iat':int(time.time()),'exp':int(time.time())+3600}
key = b'default-dev-secret-DO-NOT-USE-IN-PRODUCTION'
key = key.ljust(32, b'\x00')
print(jwt.encode(payload, key, algorithm='HS256'))
" 2>/dev/null || echo "")

if [ -n "$TOKEN" ]; then
    curl -sf -X POST "http://localhost:${PYTHON_PORT}/v1/chat/completions" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"model":"coding-agent","stream":false,"messages":[{"role":"user","content":"hi"}]}' \
        > /dev/null 2>&1 || true
    curl -sf -X POST "http://localhost:${JAVA_PORT}/v1/chat/completions" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"model":"coding-agent","stream":false,"messages":[{"role":"user","content":"hi"}]}' \
        > /dev/null 2>&1 || true
    echo "  ✅ LLM warmed up"
else
    echo "  ⚠️  Skipping warmup (PyJWT not available)"
fi

# ── Run the experiment ───────────────────────────────────────────────
echo ""
echo "🧪 Running experiment..."
echo ""
cd "$REPO_ROOT/platform/eval-harness"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUT_DIR="runs/java-vs-python-${TIMESTAMP}"

python3 java_vs_python_experiment.py \
    --python-url "http://localhost:${PYTHON_PORT}" \
    --java-url "http://localhost:${JAVA_PORT}" \
    --runs "$RUNS" \
    --out "$OUT_DIR"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Experiment complete! Results in: ${OUT_DIR}"
echo "═══════════════════════════════════════════════════════════════"



