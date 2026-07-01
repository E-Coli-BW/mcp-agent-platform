#!/bin/bash
# Test: fire 12 MCP tool calls via stdio to trigger skill extraction (nudgeInterval=10)
set -e
cd "$(dirname "$0")/../.."  # go to repo root

SERVER="node dist/memory-ts/memory-server.js"
FIFO=$(mktemp -u)
mkfifo "$FIFO"
trap "rm -f $FIFO" EXIT

echo "═══ Skill Extraction Test ═══"
echo "Store location: ~/.mcp-local/memory-store.json"
echo ""

tool_call() {
  echo '{"jsonrpc":"2.0","id":'$1',"method":"tools/call","params":{"name":"'$2'","arguments":'$3'}}'
}

# Start server with FIFO as stdin so we can control when it exits
$SERVER < "$FIFO" > /dev/null 2> /tmp/skill-stderr.txt &
SERVER_PID=$!

# Open FIFO for writing (keeps it open)
exec 3>"$FIFO"

# Initialize
echo '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"skill-test","version":"1.0"}}}' >&3
sleep 0.5
echo '{"jsonrpc":"2.0","method":"notifications/initialized"}' >&3
sleep 0.3

echo "Sending 12 tool calls..."
for i in $(seq 1 12); do
  case $i in
    1) tool_call $i memory_set '{"key":"debug-maven-build","content":"Maven build failing with stale JAR in local repo. Fixed by running mvn clean install on mcp-common before memory-server.","tags":["debug","maven","build"],"namespace":"skills"}' >&3 ;;
    2) tool_call $i memory_set '{"key":"debug-redis-pool","content":"Redis connection timeout under 50 concurrent threads. Root cause: HikariCP default pool=10 is too small. Fix: increase to 50.","tags":["debug","redis","hikari"],"namespace":"skills"}' >&3 ;;
    3) tool_call $i memory_set '{"key":"jwt-role-prefix","content":"Spring Security requires ROLE_ prefix. Changed JWT roles from SERVICE to ROLE_SERVICE to fix 403 errors.","tags":["security","jwt","spring"],"namespace":"skills"}' >&3 ;;
    4) tool_call $i memory_search '{"query":"maven stale JAR build"}' >&3 ;;
    5) tool_call $i memory_search '{"query":"redis connection pool tuning"}' >&3 ;;
    6) tool_call $i memory_set '{"key":"benchmark-error-rate","content":"QPS benchmark had 100% error rate. Three root causes: 1) JWT missing ROLE_ prefix 2) HikariCP pool=10 too small 3) Rate limiter=50/s too low for benchmark.","tags":["performance","benchmark"],"namespace":"default"}' >&3 ;;
    7) tool_call $i memory_set '{"key":"occ-backoff-tuning","content":"Optimistic lock retries used 50-200ms backoff. Too slow for hot-key contention. Reduced to 10-40ms exponential backoff.","tags":["performance","jpa"],"namespace":"default"}' >&3 ;;
    8) tool_call $i memory_set '{"key":"ci-fix-triple","content":"CI had 3 bugs: 1) requirements.txt missing (use pyproject.toml) 2) ActiveProfiles overrides spring.profiles.active 3) .github/workflows not in paths trigger.","tags":["ci","github-actions"],"namespace":"default"}' >&3 ;;
    9) tool_call $i memory_context '{}' >&3 ;;
    10) tool_call $i memory_list '{}' >&3 ;;
    11) tool_call $i memory_search '{"query":"skill auto-extracted"}' >&3 ;;
    12) tool_call $i memory_get '{"key":"debug-maven-build"}' >&3 ;;
  esac
  sleep 0.2
  echo "  → Call $i/12 sent"
done

echo ""
echo "⏳ Waiting for skill extraction LLM review (up to 40s)..."

# Wait for the LLM review to complete
for i in $(seq 1 40); do
  if grep -q '🧠 Skill.*:' /tmp/skill-stderr.txt 2>/dev/null && \
     grep -qE '(created|updated|nothing to extract|failed)' /tmp/skill-stderr.txt 2>/dev/null; then
    echo ""
    echo "  ✓ Skill extraction completed!"
    break
  fi
  sleep 1
  printf "."
done
echo ""

# Close FIFO and kill server
exec 3>&-
kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true

echo ""
echo "═══ Server stderr output ═══"
cat /tmp/skill-stderr.txt
echo ""

echo "═══ Result ═══"
if [ -f "$HOME/.mcp-local/memory-store.json" ]; then
  python3 -c "
import json
store = json.load(open('$HOME/.mcp-local/memory-store.json'))
skills = [(k,v) for k,v in store.get('entries',{}).items() if 'auto-extracted' in v.get('tags',[])]
print(f'Auto-extracted skills: {len(skills)}')
for k,v in skills:
    print(f'  → {k}')
    print(f'    {v[\"content\"][:100]}...')
    print()

all_entries = list(store.get('entries',{}).keys())
print(f'Total entries in store: {len(all_entries)}')
" 2>/dev/null || echo "Could not parse store"
else
  echo "No store file found"
fi
