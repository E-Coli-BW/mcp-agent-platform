# Model Router MCP Server

Multi-provider LLM router with cost accounting and token budget tracking.

## Quick Start
```bash
# Install mcp-common first
cd ../mcp-common && command mvn install -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml -Dmaven.repo.local=../model-router/tmp-m2-repo

# Build & Run
cd ../model-router
command mvn spring-boot:run -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml -Dmaven.repo.local=./tmp-m2-repo
```

## Tools
| Tool | Description |
|------|-------------|
| `llm_complete` | Code/text completion via best available model |
| `llm_summarize` | Summarize text to fit token budget |
| `llm_explain` | Explain code/error in natural language |
| `llm_models` | List available models, status, and costs |

## Architecture
- `LlmProvider` SPI — pluggable providers (OpenAI, Anthropic, Ollama)
- Model selection based on task type, cost, and availability
- Token budget tracking per session
- Per-tenant cost accounting
- Fallback chain: primary → secondary → local

## Port: 8480
