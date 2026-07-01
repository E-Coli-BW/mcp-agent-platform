from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Agent server configuration — loads from env vars or .env file."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8500
    debug: bool = False

    # CORS — comma-separated origins, e.g. "http://localhost:3000,https://app.example.com"
    # Default "*" for local dev; override in production.
    cors_origins: str = "*"

    # LLM
    default_model: str = "qwen2.5:7b"
    strong_model: str = "qwen2.5:7b"
    cheap_model: str = "mlx/Qwen2.5-0.5B-Instruct-4bit"
    llm_timeout: int = 60
    max_agent_steps: int = 20
    max_tokens: int = 4096
    # Default sampling temperature. Used by `_create_chat_model` when no
    # per-request override is supplied. 0.7 preserves historical behaviour
    # for production chat; eval runs typically override to 0.0 via the
    # OpenAI-compat `temperature` field on the chat completion request.
    # NOTE: changing this restarts every cached agent (cache key includes
    # the temperature) — see graph.py::get_agent.
    default_temperature: float = 0.7
    max_tool_output: int = 3000  # truncate tool results to save context window
    max_context_chars: int = 20_000  # ~5K tokens history budget for prompt modifier
    prompt_version: str = "v2"
    # Prompt governance controls.
    # - prompt_tenant_versions_json: JSON map {"tenant-id": "v1|v2|..."}
    # - prompt_canary_*: gradual rollout to a subset of requests
    # - prompt_allow_request_override: allow API clients to request a specific prompt version
    prompt_tenant_versions_json: str = "{}"
    prompt_canary_enabled: bool = False
    prompt_canary_percent: int = 0
    prompt_canary_version: str = ""
    prompt_canary_tenants: str = ""  # comma-separated tenant allowlist; empty = all tenants
    prompt_allow_request_override: bool = False
    agent_config_dir: str = "agents"
    agent_graph_version: str = "v2"  # "v1" (create_react_agent) or "v2" (explicit StateGraph)

    # ── RAG reranker strategy ──────────────────────────────────────────────
    # auto          → cross_encoder if available, else heuristic
    # llm           → use a small LLM listwise reranker (highest quality, ~500ms)
    # cross_encoder → ms-marco-MiniLM-L-6-v2 (deterministic, ~100ms)
    # heuristic     → lexical bonuses on top of RRF (legacy, fast, less accurate)
    # none          → pass through (return top_k from RRF)
    rerank_strategy: str = "auto"
    rerank_llm_model: str = ""  # blank → reuse cheap_model
    rerank_candidate_pool: int = 20  # how many to rerank
    rerank_max_passage_chars: int = 400
    rerank_timeout_seconds: float = 5.0

    # ── Reflexion / self-critique (C1) ────────────────────────────────────
    # When enabled, the agent's final answer (the LLM turn that produced
    # no tool calls) passes through a critic node BEFORE being returned.
    # The critic grades the answer 1-5 on a single composite axis
    # (correctness + completeness + evidence). If the grade falls below
    # ``reflexion_min_grade``, a HumanMessage describing the critique is
    # appended to the message history and the agent is invoked again,
    # up to ``reflexion_max_attempts`` total revision passes.
    #
    # WHY off by default: reflexion adds at least one extra LLM call per
    # request that completes without tool use. We want it explicitly
    # opted-in (per-tenant or per-config) until we measure the cost
    # delta vs the quality lift on real eval cases.
    #
    # WHY cap at 2: empirically, reflexion has sharply diminishing
    # returns past 2 attempts — the model starts rephrasing the same
    # answer rather than fixing it. 2 attempts catches the cases where
    # the LLM missed an obvious aspect on first pass; more attempts
    # mostly burn tokens.
    #
    # WHY `reflexion_model` blank by default: reuse cheap_model so the
    # critic is cheaper than the actor. The critic doesn't need tools
    # or long context — it just needs to grade. A dedicated model knob
    # is provided for future A/B experiments.
    reflexion_enabled: bool = False
    reflexion_max_attempts: int = 2
    reflexion_model: str = ""  # blank → reuse cheap_model
    reflexion_min_grade: int = 3  # grades < this trigger a revision

    # ── Direct tool routing (C2) ───────────────────────────────────────────
    # When enabled, the agent inspects the user's first message of a turn
    # via a small regex classifier. If the message is an unambiguous
    # single-tool READ (e.g. "search my memory for X", "read README.md"),
    # the router synthesizes the tool call directly and skips the first
    # call_llm hop. The LLM then sees the tool result on the next hop
    # and decides whether to continue.
    #
    # WHY off by default: the router is fast-path optimization. Until we
    # have measured agreement vs the LLM on real traffic for a given
    # tenant's prompt distribution, we can't safely assume the regex
    # rules cover their usage patterns. Opt-in keeps existing deployments
    # identical.
    #
    # WHY only READS: the router has a hardcoded allowlist (see
    # tool_router.READ_ONLY_TOOL_ALLOWLIST). Auto-routing writes or code
    # execution has unbounded blast radius if a regex misfires; auto-
    # routing reads at worst wastes one tool call. Asymmetric costs →
    # asymmetric defaults.
    #
    # WHY pure regex (no LLM): adds zero latency on the hot path and is
    # debuggable. An LLM router can be added behind a SEPARATE flag for
    # comparison via classifier_eval.py; the regex stays as the always-
    # on baseline.
    direct_tool_routing_enabled: bool = False

    # ── Subagent verifier (C3) ─────────────────────────────────────────────
    # When enabled, every subagent answer is graded by a cheap verifier
    # model BEFORE bubbling back to the parent. Pairs with C1 reflexion
    # for "defense in depth": the parent self-critiques its own final
    # answer (C1) AND every subagent answer is independently graded
    # against its brief (C3). Two quality gates on different message
    # graphs with different prompts.
    #
    # WHY separate from C1: the C1 critic sees (user_question, agent_answer);
    # the C3 verifier sees (parent_brief, child_answer). Different rubrics
    # because the grading question is different. Sharing code would
    # conflate "user question" with "parent brief" and produce confusing
    # wire-format ambiguity in the logs.
    #
    # WHY auto-retry is bounded to 1: subagents already run 5-20s.
    # A retry doubles that. Beyond 1 the marginal quality lift doesn't
    # justify the latency. Setting to False ships the ⚠️-marked answer
    # immediately and lets the parent LLM decide what to do with it.
    #
    # WHY off by default: same opt-in stance as C1/C2. Add the gate to
    # the pipeline, gate the behavior. Zero behavior change until the
    # cost/quality trade is measured per-deployment.
    #
    # WHY `subagent_verifier_model` blank by default: reuse cheap_model
    # (same pattern as reflexion_model and rerank_llm_model).
    subagent_verifier_enabled: bool = False
    subagent_verifier_model: str = ""  # blank → reuse cheap_model
    subagent_verifier_min_grade: int = 3  # grades < this → failed
    subagent_verifier_auto_retry: bool = True  # if False, just mark with ⚠️

    # ── Skills index injection (catalog + lazy load) ───────────────────────
    # When enabled, the agent's system prompt receives a COMPACT catalog of
    # available skills (key + 1-line summary + tags), NOT full skill bodies.
    # The LLM then calls `skill_get(key)` to lazily fetch any skill it wants.
    # This keeps context bounded even with 100s of skills.
    skills_index_enabled: bool = True
    skills_namespace: str = "skills"
    skills_index_max_entries: int = 40  # cap injected catalog size
    skills_index_summary_chars: int = 100  # per-entry summary length
    skills_index_ttl_seconds: int = 60  # cache TTL

    # Ollama
    ollama_base_url: str = "http://localhost:11434"

    # MLX local server (Apple Silicon native, see platform/llm-infra/)
    mlx_base_url: str = "http://localhost:8600"

    # OpenAI (optional)
    openai_api_key: str = ""
    # Anthropic (optional)
    anthropic_api_key: str = ""

    # LLM Fallback — automatic failover when primary model is down
    # Format: same as default_model (e.g., "deepseek-chat", "openai/gpt-4o-mini")
    # Empty string = no fallback (fail immediately on primary failure)
    fallback_model: str = ""
    model_circuit_breaker_threshold: int = 5   # failures before circuit opens
    model_circuit_breaker_cooldown: float = 60.0  # seconds before half-open retry

    # MCP Tool backends
    memory_server_url: str = "http://localhost:8180"
    filesearch_server_url: str = "http://localhost:8280"
    codeexec_server_url: str = "http://localhost:8380"

    # Auth service (for JWKS verification)
    auth_service_url: str = "http://localhost:8090"

    # Redis — single URL for all uses (conversation, RAG vector, cache)
    # MUST use db 0 because RediSearch FT.CREATE only works on db 0 (Pitfall #28)
    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 1800  # 30 min

    # Auth — no default in production! Fail fast if not set.
    # Set AGENT_JWT_SECRET env var or use dev default below.
    jwt_secret: str = "default-dev-secret-DO-NOT-USE-IN-PRODUCTION"

    # Kafka — event streaming for tool audit + analytics
    # Set to empty string to disable Kafka
    kafka_bootstrap_servers: str = "localhost:9093"

    # OpenTelemetry — distributed tracing
    # Set to OTLP endpoint (e.g., "http://localhost:4317") or empty to use console exporter
    otlp_endpoint: str = ""

    # Ticket backend — "local" (markdown files) or "jira" (Atlassian REST API)
    ticket_backend: str = "local"

    # Jira settings (only needed when ticket_backend=jira)
    jira_url: str = ""          # e.g., https://company.atlassian.net
    jira_email: str = ""
    jira_token: str = ""        # API token from Atlassian account
    jira_project: str = "OPS"   # Jira project key
    jira_issue_type: str = "Bug"

    model_config = {"env_prefix": "AGENT_", "env_file": ".env"}


settings = Settings()
