"""Logging configuration — structured, readable agent loop logs.

Usage:
    Set AGENT_LOG_LEVEL=DEBUG in .env for full agent loop visibility.
    Default: INFO (shows request/response + tool decisions, not internals).

Log output example (DEBUG):
    17:27:43.849 [agent.chat] INFO  📩 User message received [session=s-abc, model=ops-agent]: fix the OOM issue
    17:27:43.850 [agent.graph] INFO  📂 Workspace context injected (2340 chars)
    17:27:43.851 [agent.graph] INFO  🧠 LLM call [loop=1, msgs=3, chars=2800, errors=0]
    17:27:44.200 [agent.graph] INFO  🤖 LLM decided to call 1 tool(s): ['file_search'] [349ms]
    17:27:44.201 [agent.graph] DEBUG    🔧 file_search({'query': 'OOM heap memory'})
    17:27:44.350 [agent.graph] INFO     ✅ Tool result [file_search, 1200 chars]: 3 matches found...
    17:27:44.351 [agent.graph] INFO  🧠 LLM call [loop=2, msgs=5, chars=4100, errors=0]
    17:27:45.100 [agent.graph] INFO  🤖 LLM decided to call 1 tool(s): ['ticket_create'] [749ms]
    17:27:45.110 [agent.graph] INFO     ✅ Tool result [ticket_create, 65 chars]: ✅ Ticket created: INC-001
    17:27:45.111 [agent.graph] INFO  🧠 LLM call [loop=3, msgs=7, chars=4500, errors=0]
    17:27:45.800 [agent.graph] INFO  🤖 LLM responded with text [689ms, 230 chars]: Created ticket INC-001...
    17:27:45.801 [agent.chat] INFO  📤 Agent response [session=s-abc, tools=2, 1952ms]: Created ticket INC-001...
"""

import logging
import os
import sys


def configure_logging():
    """Configure logging for the agent server.

    Call this once at startup (main.py lifespan).
    """
    log_level = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()

    # Format: timestamp [logger] LEVEL message
    fmt = "%(asctime)s.%(msecs)03d [%(name)s] %(levelname)-5s %(message)s"
    datefmt = "%H:%M:%S"

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=fmt,
        datefmt=datefmt,
        stream=sys.stderr,
        force=True,  # Override any existing config
    )

    # Set specific logger levels
    agent_level = getattr(logging, log_level, logging.INFO)

    # Agent internals — the logs you want to see
    logging.getLogger("agent.chat").setLevel(agent_level)
    logging.getLogger("app.agent.graph_v2").setLevel(agent_level)
    logging.getLogger("app.agent.graph").setLevel(agent_level)
    logging.getLogger("app.tools").setLevel(agent_level)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("langchain").setLevel(logging.WARNING)
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "📋 Logging configured: level=%s", log_level,
    )
