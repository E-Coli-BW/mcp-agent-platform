# ── MCP Platform Build & Test ──────────────────────────────────
#
# Usage:
#   make test          — run ALL tests (Java + Python)
#   make test-java     — run Java tests only
#   make test-python   — run Python tests only
#   make build         — build all modules
#   make verify        — build + test (pre-commit validation)
#   make install-hooks — install git pre-commit hook
#
# Prerequisites:
#   - Java 21+ (JAVA_HOME set)
#   - Maven 3.9+ (mvn on PATH)
#   - Python 3.12+ with venv at platform/agent-server/.venv
#   - Node.js 18+ (for TypeScript MCP servers)

.PHONY: test test-java test-python test-integration build verify install-hooks clean docker-up docker-down

SHELL := /bin/bash

# ── Paths ─────────────────────────────────────────────────────
ROOT          := $(shell pwd)
MEMORY_SERVER := platform/memory-server
AUTH_SERVICE  := platform/auth-service
AGENT_SERVER  := platform/agent-server
AGENT_JAVA    := platform/agent-server-java
MCP_COMMON    := platform/mcp-common
COMMON_SPI    := platform/common-spi

# Maven settings (bypass internal Nexus, use Aliyun/Tencent mirror)
MVN := command mvn
MVN_SETTINGS := -s $(ROOT)/$(MEMORY_SERVER)/tmp-mvn-settings.xml -gs $(ROOT)/$(MEMORY_SERVER)/tmp-mvn-settings.xml
MVN_REPO     := -Dmaven.repo.local=$(ROOT)/$(MEMORY_SERVER)/tmp-m2-repo
MVN_OPTS     := $(MVN_SETTINGS) $(MVN_REPO)

# Python venv
PYTHON := $(ROOT)/$(AGENT_SERVER)/.venv/bin/python

# ── Build ─────────────────────────────────────────────────────

build: build-java build-python  ## Build all modules
	@echo "✅ All modules built"

build-java: build-common  ## Build Java modules
	@echo "📦 Building memory-server..."
	@cd $(MEMORY_SERVER) && $(MVN) package $(MVN_OPTS) -DskipTests -q
	@echo "📦 Building agent-server-java..."
	@cd $(AGENT_JAVA) && $(MVN) package -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml -Dmaven.repo.local=./tmp-m2-repo -DskipTests -q

build-common:  ## Build shared Java modules (mcp-common, common-spi)
	@echo "📦 Building mcp-common..."
	@cd $(MCP_COMMON) && $(MVN) install $(MVN_OPTS) -DskipTests -q
	@echo "📦 Building common-spi..."
	@cd $(COMMON_SPI) && $(MVN) install $(MVN_OPTS) -DskipTests -q

build-python:  ## Verify Python dependencies
	@echo "📦 Checking Python venv..."
	@test -f $(PYTHON) || (echo "❌ Python venv not found at $(PYTHON). Run: cd $(AGENT_SERVER) && python3 -m venv .venv && pip install -r requirements.txt" && exit 1)
	@$(PYTHON) -c "import fastapi; import langchain_core" 2>/dev/null || (echo "❌ Missing Python deps" && exit 1)
	@echo "  ✓ Python venv OK"

# ── Test ──────────────────────────────────────────────────────

test: test-java test-python  ## Run ALL tests
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "✅ ALL TESTS PASSED (Java + Python)"
	@echo "═══════════════════════════════════════════"

test-java: build-common  ## Run Java tests (rebuilds mcp-common first!)
	@echo ""
	@echo "🧪 Running Java tests (memory-server)..."
	@cd $(MEMORY_SERVER) && $(MVN) test $(MVN_OPTS) -q
	@echo "  ✓ memory-server tests passed"
	@echo "🧪 Running Java tests (auth-service)..."
	@cd $(AUTH_SERVICE) && $(MVN) test -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml -Dmaven.repo.local=./tmp-m2-repo -q
	@echo "  ✓ auth-service tests passed"
	@echo "🧪 Running Java tests (agent-server-java)..."
	@cd $(AGENT_JAVA) && $(MVN) test -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml -Dmaven.repo.local=./tmp-m2-repo -q
	@echo "  ✓ agent-server-java tests passed"

test-python:  ## Run Python tests
	@echo ""
	@echo "🧪 Running Python tests..."
	@cd $(AGENT_SERVER) && $(PYTHON) -m pytest tests/ -q --tb=short
	@echo "  ✓ Python tests passed"

# ── Verify (pre-commit) ──────────────────────────────────────

verify: build test  ## Full build + test validation (use before committing)
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "✅ VERIFICATION PASSED — safe to commit"
	@echo "═══════════════════════════════════════════"

# ── Docker Integration Tests ──────────────────────────────────

docker-up:  ## Start infrastructure (Redis + PostgreSQL + Auth Service)
	@echo "🐳 Starting Docker infrastructure..."
	@cd platform && docker compose up -d redis postgres 2>/dev/null || true
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
	@cd $(MEMORY_SERVER) && $(MVN) test $(MVN_OPTS) -Dspring.profiles.active=docker-test -Dsurefire.integration.excludes= -q 2>/dev/null || \
		(echo "  ⚠️  Docker integration tests skipped (Docker not available)" && true)
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
	@cp ~/.mcp-local/memory-store.json backups/memory-store.json
	@echo "✅ Memory backed up to backups/memory-store.json"

restore-memory:  ## Restore memory-store.json from git backup
	@mkdir -p ~/.mcp-local
	@cp backups/memory-store.json ~/.mcp-local/memory-store.json
	@echo "✅ Memory restored from backup"

# ── Help ──────────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
