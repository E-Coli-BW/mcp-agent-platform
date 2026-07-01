# Observing the Agent Loop — A Practical Tutorial

This guide shows you how to use the structured logging system to understand what
your agent is doing in real time.

## Quick Start

```bash
# Terminal 1: Start the agent server with DEBUG logs
cd platform/agent-server
AGENT_LOG_LEVEL=DEBUG AGENT_AGENT_GRAPH_VERSION=v2 .venv/bin/python -m uvicorn app.main:app --port 8500

# Terminal 2: Generate a dev JWT token (valid 1 hour)
cd platform/agent-server
TOKEN=$(.venv/bin/python -c "import jwt,time; print(jwt.encode({'sub':'test','tenant_id':'dev','exp':int(time.time())+3600},'default-dev-secret-DO-NOT-USE-IN-PRODUCTION',algorithm='HS256'))")

# Send a test request
curl -N http://localhost:8500/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "model": "ops-agent",
    "messages": [{"role": "user", "content": "create a ticket for the OOM issue in worker-pool"}],
    "stream": true,
    "session_id": "test-session-1"
  }'
```

## What You'll See (Terminal 1)

### Level: INFO (default)

Shows the **what** — request/response and tool decisions:

```
17:27:43.849 [agent.chat]          INFO  📩 User message received [session=test-session-1, model=ops-agent]: create a ticket for the OOM issue
17:27:43.851 [app.agent.graph_v2]  INFO  📂 Workspace context injected (2340 chars)
17:27:43.852 [app.agent.graph_v2]  INFO  🧠 LLM call [loop=1, msgs=2, chars=2800, errors=0]
17:27:44.200 [app.agent.graph_v2]  INFO  🤖 LLM decided to call 1 tool(s): ['file_search'] [349ms]
17:27:44.350 [app.agent.graph_v2]  INFO     ✅ Tool result [file_search, 1200 chars]: 3 matches found...
17:27:44.351 [app.agent.graph_v2]  INFO  🧠 LLM call [loop=2, msgs=4, chars=4100, errors=0]
17:27:45.100 [app.agent.graph_v2]  INFO  🤖 LLM decided to call 1 tool(s): ['ticket_create'] [749ms]
17:27:45.110 [app.agent.graph_v2]  INFO     ✅ Tool result [ticket_create, 65 chars]: ✅ Ticket created: INC-001
17:27:45.111 [app.agent.graph_v2]  INFO  🧠 LLM call [loop=3, msgs=6, chars=4500, errors=0]
17:27:45.800 [app.agent.graph_v2]  INFO  🤖 LLM responded with text [689ms, 230 chars]: Created ticket INC-001...
17:27:45.801 [agent.chat]          INFO  📤 Agent response [session=test-session-1, tools=2, 1952ms]: Created ticket INC-001...
```

### Level: DEBUG

Shows the **how** — exact tool arguments, message types, routing decisions:

```
17:27:43.851 [app.agent.graph_v2]  DEBUG 🧠 LLM message types: ['SystemMessage', 'HumanMessage']
17:27:44.200 [app.agent.graph_v2]  DEBUG    🔧 file_search({'query': 'OOM worker-pool'})
17:27:44.201 [app.agent.graph_v2]  DEBUG ➡️  Routing to tools: ['file_search']
17:27:44.352 [app.agent.graph_v2]  DEBUG ✅ Error streak reset (was 0)
17:27:45.100 [app.agent.graph_v2]  DEBUG    🔧 ticket_create({'title': 'OOM in worker-pool', 'severity': 'high', ...})
17:27:45.801 [app.agent.graph_v2]  DEBUG ➡️  Routing to END (no tool calls)
```

## How to Read the Logs

### The ReAct Loop

Each request follows this pattern:

```
📩 User message received
  📂 Workspace context injected (once)
  🧠 LLM call [loop=1]           ← Agent thinks
  🤖 LLM decided to call tools   ← Agent acts
    🔧 tool_name(args)           ← Tool executes (DEBUG only)
    ✅ Tool result                ← Agent observes
  🧠 LLM call [loop=2]           ← Agent re-thinks with tool result
  🤖 LLM responded with text     ← Agent is done
📤 Agent response sent
```

### Key Metrics to Watch

| Log Field | What It Tells You | Red Flag |
|-----------|-------------------|----------|
| `loop=N` | How many ReAct iterations | >5 means agent is struggling |
| `msgs=N` | Messages in context window | >20 means context is getting bloated |
| `chars=N` | Total characters sent to LLM | >15000 means approaching budget |
| `errors=N` | Consecutive tool failures | >0 means something is broken |
| `[Xms]` | LLM inference latency | >5000ms means model is too slow |
| `tools=N` | Total tools called per request | >10 means agent is inefficient |

### Error Patterns

**Tool error with retry:**
```
17:27:44.350 [app.agent.graph_v2]  INFO     ❌ Tool result [file_read, 30 chars]: ❌ File not found: bad.py
17:27:44.351 [app.agent.graph_v2]  WARN  ⚠️  Tool error detected (consecutive: 1/3)
17:27:44.500 [app.agent.graph_v2]  INFO  🧠 LLM call [loop=2, msgs=5, chars=3200, errors=1]
17:27:45.200 [app.agent.graph_v2]  INFO  🤖 LLM decided to call 1 tool(s): ['file_list']  ← recovered!
```

**Agent forced to stop:**
```
17:27:50.000 [app.agent.graph_v2]  WARN  🛑 Agent hit max steps (20/20), forcing end
```

**Context compression:**
```
17:27:46.000 [app.agent.graph_v2]  DEBUG 📦 Compressed tool output [file_read]: 5000 → 1500 chars
17:27:46.001 [app.agent.graph_v2]  INFO  📦 Compressed 3 old tool outputs
```

## Log Levels Cheat Sheet

| Level | Use Case | What You See |
|-------|----------|--------------|
| `WARNING` | Production | Only errors, max-steps, forced stops |
| `INFO` | Daily ops | Request/response, tool decisions, timings |
| `DEBUG` | Development | Everything: args, message types, routing, compression |

## Configuration

```bash
# .env file
AGENT_LOG_LEVEL=DEBUG              # Log level (DEBUG/INFO/WARNING)
AGENT_AGENT_GRAPH_VERSION=v2       # Use v2 graph (has structured logging)

# Or as environment variables
export AGENT_LOG_LEVEL=DEBUG
```

## Filtering Logs

```bash
# Only see the ReAct loop (no startup noise)
AGENT_LOG_LEVEL=DEBUG .venv/bin/python -m uvicorn app.main:app 2>&1 | grep -E '🧠|🤖|🔧|✅|❌|📩|📤'

# Only see tool calls
AGENT_LOG_LEVEL=DEBUG .venv/bin/python -m uvicorn app.main:app 2>&1 | grep '🔧'

# Only see errors
.venv/bin/python -m uvicorn app.main:app 2>&1 | grep -E 'WARN|ERROR|🛑|❌'

# Save logs to file for later analysis
AGENT_LOG_LEVEL=DEBUG .venv/bin/python -m uvicorn app.main:app 2>agent.log
tail -f agent.log | grep -E '🧠|🤖'
```

## Tracing with Jaeger (Optional)

For visual trace trees across Python agent → Java backends:

```bash
# Start Jaeger
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one:latest

# Start agent with OTLP export
AGENT_OTLP_ENDPOINT=http://localhost:4317 .venv/bin/python -m uvicorn app.main:app

# Open Jaeger UI
open http://localhost:16686
# Select service "agent-server" → Find Traces
```

## What to Look For When Debugging

### Agent is slow
→ Check `[Xms]` on LLM calls. If >3s, consider a faster model or reducing context.

### Agent calls too many tools
→ Check `loop=N`. If consistently >5, your prompt may be too vague. Look at DEBUG logs
to see what tools it's calling and why.

### Agent gives wrong answers
→ Set `DEBUG`, look at `🧠 LLM message types` — is the context window full of old
irrelevant tool outputs? Check `📦 Compressed` lines to see if compression is happening.

### Agent loops on errors
→ Look for `⚠️ Tool error detected (consecutive: N/3)`. If it hits 3/3, the agent
stops. Check the `❌ Tool result` lines to see what's failing.
