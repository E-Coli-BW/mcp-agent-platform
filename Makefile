# ── MCP Platform Build & Test ──────────────────────────────────
#
# Usage:
#   make test          — run ALL tests (Java + Python)
#   make test-java     — run Java tests only
#   make test-python   — run Python tests only
#   make test-typescript — build and test TypeScript MCP servers
#   make test-frontend — type-check and build the React IDE
#   make test-python-contract — run context state/model-view contract gate
#   make test-agent-runtime-contract — run approval/cache/error runtime contracts
#   make build         — build all modules
#   make verify        — build + test (pre-commit validation)
#   make install-hooks — install git pre-commit hook
#
# Prerequisites:
#   - Java 21+ (JAVA_HOME set)
#   - Maven 3.9+ (mvn on PATH)
#   - Python 3.12+ with venv at platform/agent-server/.venv
#   - Node.js 18+ (for TypeScript MCP servers)

.PHONY: test test-java test-python test-typescript test-frontend test-python-contract test-agent-runtime-contract test-integration build verify install-hooks clean docker-up docker-down

SHELL := /bin/bash

# ── Paths ─────────────────────────────────────────────────────
ROOT          := $(shell pwd)
MEMORY_SERVER := platform/memory-server
AUTH_SERVICE  := platform/auth-service
AGENT_SERVER  := platform/agent-server
AGENT_JAVA    := platform/agent-server-java
MCP_COMMON    := platform/mcp-common
COMMON_SPI    := platform/common-spi
MCP_CLI       := platform/mcp-cli
FRONTEND      := platform/frontend

# Maven settings are optional: prefer the tracked root file, then CI's generated file.
MVN := command mvn
MVN_SETTINGS_FILE := $(firstword $(wildcard $(ROOT)/tmp-mvn-settings.xml $(ROOT)/$(MEMORY_SERVER)/tmp-mvn-settings.xml))
MVN_SETTINGS := $(if $(MVN_SETTINGS_FILE),-s $(MVN_SETTINGS_FILE) -gs $(MVN_SETTINGS_FILE))
# Local builds reuse ~/.m2 by default. CI sets MAVEN_LOCAL_REPO to keep its
# repository cache isolated and reproducible.
MAVEN_LOCAL_REPO ?=
MVN_REPO     := $(if $(strip $(MAVEN_LOCAL_REPO)),-Dmaven.repo.local=$(abspath $(MAVEN_LOCAL_REPO)))
MVN_OPTS     := $(MVN_SETTINGS) $(MVN_REPO) -B -ntp

# Python venv
PYTHON := $(ROOT)/$(AGENT_SERVER)/.venv/bin/python

# ── Build ─────────────────────────────────────────────────────

build: build-java build-python  ## Build all modules
	@echo "✅ All modules built"

build-java: build-common  ## Build Java modules
	@echo "📦 Building memory-server..."
	@cd $(MEMORY_SERVER) && $(MVN) package $(MVN_OPTS) -DskipTests
	@echo "📦 Building agent-server-java..."
	@cd $(AGENT_JAVA) && $(MVN) package $(MVN_OPTS) -DskipTests

build-common:  ## Build shared Java modules (mcp-common, common-spi)
	@echo "📦 Building mcp-common..."
	@cd $(MCP_COMMON) && $(MVN) install $(MVN_OPTS) -DskipTests
	@echo "📦 Building common-spi..."
	@cd $(COMMON_SPI) && $(MVN) install $(MVN_OPTS) -DskipTests

build-python:  ## Verify Python dependencies
	@echo "📦 Checking Python venv..."
	@test -f $(PYTHON) || (echo "❌ Python venv not found at $(PYTHON). Run: cd $(AGENT_SERVER) && python3 -m venv .venv && pip install -r requirements.txt" && exit 1)
	@$(PYTHON) -c "import fastapi; import langchain_core" 2>/dev/null || (echo "❌ Missing Python deps" && exit 1)
	@echo "  ✓ Python venv OK"

# ── Test ──────────────────────────────────────────────────────

test: test-java test-python test-typescript test-frontend  ## Run ALL tests
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "✅ ALL TESTS PASSED (Java + Python + TypeScript + Frontend)"
	@echo "═══════════════════════════════════════════"

test-java: build-common  ## Run Java tests (rebuilds mcp-common first!)
	@echo ""
	@echo "🧪 Running Java tests (memory-server)..."
	@cd $(MEMORY_SERVER) && $(MVN) test $(MVN_OPTS) -q
	@echo "  ✓ memory-server tests passed"
	@echo "🧪 Running Java tests (auth-service)..."
	@cd $(AUTH_SERVICE) && $(MVN) test $(MVN_OPTS) -q
	@echo "  ✓ auth-service tests passed"
	@echo "🧪 Running Java tests (agent-server-java)..."
	@cd $(AGENT_JAVA) && $(MVN) test $(MVN_OPTS) -q
	@echo "  ✓ agent-server-java tests passed"

test-python:  ## Run Python tests
	@echo ""
	@echo "🧪 Running Python tests..."
	@cd $(AGENT_SERVER) && $(PYTHON) -m pytest tests/ -q --tb=short
	@echo "  ✓ Python tests passed"

test-typescript:  ## Build and test TypeScript MCP servers
	@echo ""
	@echo "🧪 Running TypeScript MCP tests..."
	@cd $(MCP_CLI) && npm test
	@echo "  ✓ TypeScript MCP tests passed"

test-frontend:  ## Type-check and build the React IDE
	@echo ""
	@echo "🧪 Building frontend..."
	@cd $(FRONTEND) && npm run build
	@echo "  ✓ Frontend build passed"

test-python-contract:  ## Enforce context state, checkpoint, model-view, and docs contracts
	@echo ""
	@echo "🔒 Running context state contract gate..."
	@cd $(AGENT_SERVER) && $(PYTHON) -m ruff check \
		app/agent/compressor app/agent/agent_graph \
		tests/conftest.py tests/test_compressor.py tests/test_graph_v2.py \
		tests/test_context_contract.py
	@cd $(AGENT_SERVER) && $(PYTHON) -m pytest \
		tests/test_compressor.py tests/test_graph_v2.py tests/test_context_contract.py \
		-q --tb=short
	@echo "  ✓ Context state contract passed"

test-agent-runtime-contract:  ## Enforce approval, cache isolation, and tool error contracts
	@echo ""
	@echo "🔒 Running Agent runtime contract gate..."
	@cd $(AGENT_SERVER) && $(PYTHON) -m ruff check \
		app/runtime/checkpoint.py app/runtime/continuation.py \
		app/runtime/tool_authorization.py app/api/chat.py \
		app/agent/engine app/agent/agent_graph app/agent/delegation \
		app/tools/mcp_client.py tests/test_runtime_tool_authorization.py \
		tests/test_runtime_approval_api.py \
		tests/test_runtime_checkpoint.py tests/test_runtime_checkpoint_transitions.py \
		tests/test_runtime_continuation.py tests/test_runtime_continuation_async.py \
		tests/test_auth_contract.py tests/test_temperature_plumbing.py
	@cd $(AGENT_SERVER) && $(PYTHON) -m pytest \
		tests/test_runtime_tool_authorization.py \
		tests/test_runtime_approval_api.py \
		tests/test_runtime_approval.py tests/test_runtime_approval_pause.py \
		tests/test_runtime_approval_store.py tests/test_runtime_approval_audit.py \
		tests/test_runtime_checkpoint.py tests/test_runtime_checkpoint_transitions.py \
		tests/test_runtime_continuation.py tests/test_runtime_continuation_async.py \
		tests/test_runtime_approval_recovery_script.py tests/test_auth_contract.py \
		tests/test_lineage_propagation.py tests/test_temperature_plumbing.py \
		tests/test_prompt_cache_isolation.py tests/test_domain_pack.py \
		tests/test_rag_tool_contract.py tests/test_rag_tenant_isolation.py \
		-q --tb=short
	@echo "  ✓ Agent runtime contract passed"

# ── Verify (pre-commit) ──────────────────────────────────────

verify: build test-python-contract test-agent-runtime-contract test  ## Full build + test validation (use before committing)
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "✅ VERIFICATION PASSED — safe to commit"
	@echo "═══════════════════════════════════════════"

# ── Docker Integration Tests ──────────────────────────────────

docker-up:  ## Start infrastructure (Redis + PostgreSQL + Auth Service)
	@echo "🐳 Starting Docker infrastructure..."
	@command -v docker >/dev/null || (echo "❌ Docker is required for integration tests" && exit 1)
	@docker info >/dev/null 2>&1 || (echo "❌ Docker daemon is not available" && exit 1)
	@cd platform && docker compose up -d redis postgres
	@echo "  Waiting for services to be healthy..."
	@sleep 5
	@echo "  ✓ Docker infrastructure ready"

docker-down:  ## Stop Docker infrastructure
	@echo "🐳 Stopping Docker infrastructure..."
	@cd platform && docker compose down 2>/dev/null || true
	@echo "  ✓ Docker stopped"

test-integration: docker-up build-common  ## Run integration tests against real Docker infrastructure
	@echo ""
	@echo "🧪 Running integration tests (requires Docker)..."
	@cd $(MEMORY_SERVER) && $(MVN) test $(MVN_OPTS) \
		-Dspring.profiles.active=docker-test -Dsurefire.integration.excludes= -q
	@echo "  ✓ Integration tests complete"

test-all: test test-integration  ## Run unit + integration tests
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "✅ ALL TESTS PASSED (unit + integration)"
	@echo "═══════════════════════════════════════════"

benchmark: docker-up build-common  ## Run QPS benchmark (requires Docker)
	@echo ""
	@echo "🏋️ Running QPS benchmark (requires Docker infrastructure)..."
	@cd $(MEMORY_SERVER) && $(MVN) test $(MVN_OPTS) \
		-Dspring.profiles.active=integration-test \
		-Dtest=MemoryBenchmark \
		-Dsurefire.excludedGroups=
	@echo "  ✓ Benchmark complete"
	@echo "  📄 Report: $(ROOT)/$(MEMORY_SERVER)/target/benchmark-results.json"

# ── Git Hooks ─────────────────────────────────────────────────

install-hooks:  ## Install git pre-commit hook
	@cp scripts/pre-commit .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "✅ Pre-commit hook installed"
	@echo "   Every commit will run: make verify"
	@echo "   To skip (emergency): git commit --no-verify"

# ── Clean ─────────────────────────────────────────────────────

clean:  ## Clean build artifacts
	@cd $(MEMORY_SERVER) && $(MVN) clean $(MVN_OPTS) -q 2>/dev/null || true
	@cd $(MCP_COMMON) && $(MVN) clean $(MVN_OPTS) -q 2>/dev/null || true
	@cd $(COMMON_SPI) && $(MVN) clean $(MVN_OPTS) -q 2>/dev/null || true
	@rm -rf $(AGENT_SERVER)/.pytest_cache $(AGENT_SERVER)/__pycache__
	@echo "✅ Clean complete"

# ── Memory Backup ─────────────────────────────────────────────

backup-memory:  ## Backup memory-store.json to git
	@mkdir -p backups
	@cp ~/.mcp-local/memory-store.json backups/memory-store.json
	@echo "✅ Memory backed up to backups/memory-store.json"

restore-memory:  ## Restore memory-store.json from git backup
	@mkdir -p ~/.mcp-local
	@cp backups/memory-store.json ~/.mcp-local/memory-store.json
	@echo "✅ Memory restored from backup"

# ── Help ──────────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
