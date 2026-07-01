"""Coding Agent Server — OpenAI-compatible API with LangGraph agent."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.auth_proxy import router as auth_proxy_router
from app.api.chat import router as chat_router
from app.api.workspace import router as workspace_router
from app.config import settings
from app.middleware.jwt_auth import JwtAuthMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.tracing import TracingMiddleware

logger = logging.getLogger(__name__)

# Startup security check
if "DO-NOT-USE" in settings.jwt_secret:
    if os.environ.get("AGENT_ENV", "dev").lower() in ("prod", "production", "staging"):
        raise RuntimeError(
            "🚨 FATAL: Default JWT secret detected in production! "
            "Set AGENT_JWT_SECRET env var before starting."
        )
    logger.warning(
        "⚠️  Using default JWT secret! Set AGENT_JWT_SECRET env var for production."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle events."""
    import asyncio

    # Configure structured logging first
    from app.logging_config import configure_logging
    configure_logging()

    # Pre-warm JWKS cache so the first protected request doesn't pay the
    # 3 s auth-service round-trip itself (which would also have blocked
    # the asyncio event loop under the old urllib implementation).
    try:
        from app.middleware.jwt_auth import prewarm_jwks
        auth_url = os.environ.get("AUTH_SERVICE_URL", "http://localhost:8090")
        await prewarm_jwks(auth_url)
    except Exception as e:
        logger.warning("JWKS prewarm skipped: %s", e)

    # Register builtin tools
    try:
        from app.registry.tool_registry import register_all_builtins

        register_all_builtins()
    except Exception as e:
        logger.warning("Tool registry init failed: %s", e)

    # Load plugins
    try:
        from app.plugins.loader import load_plugins

        plugins_dir = os.environ.get("AGENT_PLUGINS_DIR", "plugins")
        load_plugins(plugins_dir)
    except Exception as e:
        logger.warning("Plugin loading failed: %s", e)

    # Start config watcher
    def on_config_change(changed_path: str):
        from app.agent.graph import _agent_tool_names, _agents
        # Atomic swap: build new empty dicts and replace references.
        # In-flight requests still hold refs to the old dict entries
        # and will complete normally. New requests get fresh agents.
        import app.agent.graph as _graph_mod
        _graph_mod._agents = {}
        _graph_mod._agent_tool_names = {}
        logger.info("Agent cache atomically replaced due to config change: %s", changed_path)

    try:
        from app.registry.watcher import watch_configs
        asyncio.create_task(watch_configs(settings.agent_config_dir, on_config_change))
    except Exception as e:
        logger.warning("Config watcher failed to start: %s", e)

    yield


app = FastAPI(
    title="Coding Agent Server",
    description="OpenAI-compatible coding agent with tool use, RAG, and streaming",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for web frontends
_origins = (
    ["*"] if settings.cors_origins == "*"
    else [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting — 60 requests/minute per IP on chat endpoints
app.add_middleware(
    RateLimitMiddleware,
    max_requests=60,
    window_seconds=60,
    paths=["/v1/chat"],
)

# JWT authentication — protects /v1/** endpoints
app.add_middleware(
    JwtAuthMiddleware,
    jwt_secret=settings.jwt_secret,
    protected_prefixes=["/v1/"],
)

# OpenTelemetry tracing + metrics (outermost — wraps all other middleware)
app.add_middleware(TracingMiddleware)

# Routes
app.include_router(chat_router)
app.include_router(workspace_router)
# Auth proxy — forwards /auth/* to the auth-service so the embedded UI
# (at /ui/, same origin :8580) can use relative URLs without CORS pain.
# See app/api/auth_proxy.py for the full rationale.
app.include_router(auth_proxy_router)

# Static files (web UI) — with cache-busting for development
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    from starlette.responses import FileResponse

    @app.get("/ui/{path:path}")
    @app.get("/ui")
    @app.get("/ui/")
    async def serve_ui(path: str = ""):
        """Serve static UI files with no-cache headers for development."""
        file_path = static_dir / (path or "index.html")
        if not file_path.exists() or not file_path.is_file():
            file_path = static_dir / "index.html"
        return FileResponse(
            file_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
        )


@app.get("/health")
async def health():
    return {"status": "ok", "model": settings.default_model}


@app.get("/metrics")
async def metrics():
    """Prometheus-style metrics for monitoring dashboards."""
    from app.middleware.tracing import get_metrics_summary
    return get_metrics_summary()


@app.get("/api/prompts")
async def prompts_info():
    """Prompt version registry — available versions, canary config, tenant overrides."""
    from app.agent.prompts import _prompt_registry, _parse_tenant_overrides
    registry = _prompt_registry()
    overrides = _parse_tenant_overrides(settings.prompt_tenant_versions_json)
    return {
        "default_version": settings.prompt_version,
        "available_versions": {k: list(v.keys()) for k, v in registry.items()},
        "tenant_overrides": overrides,
        "canary": {
            "enabled": settings.prompt_canary_enabled,
            "version": settings.prompt_canary_version,
            "percent": settings.prompt_canary_percent,
            "tenants": settings.prompt_canary_tenants,
        },
        "allow_request_override": settings.prompt_allow_request_override,
    }


@app.get("/api/usage")
async def usage():
    """Token usage and cost tracking."""
    from app.usage import get_usage_tracker
    return get_usage_tracker().get_summary()


@app.get("/api/reranker")
async def reranker_info():
    """Learned reranker weights and feedback stats."""
    from app.rag.reranking.learned import get_learned_reranker
    return get_learned_reranker().get_weights_info()


@app.post("/api/reranker/retrain")
async def reranker_retrain():
    """Trigger retraining of reranker weights from accumulated feedback."""
    from app.rag.reranking.learned import get_learned_reranker
    r = get_learned_reranker()
    r.retrain(min_samples=5)
    return {"status": "retrained", **r.get_weights_info()}


@app.get("/api/reranker/evaluate")
async def reranker_evaluate():
    """Evaluate learned vs default reranker weights on held-out test set."""
    from app.rag.reranking.learned import get_learned_reranker
    result = get_learned_reranker().evaluate()
    if result is None:
        return {"error": "Insufficient feedback data (<10 samples or all same label)"}
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
