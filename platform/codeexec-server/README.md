# Code Execution MCP Server

Sandboxed code execution MCP server with Docker container isolation.

## Quick Start
```bash
command mvn clean install -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo -DskipTests

command mvn spring-boot:run -f pom.xml \
  -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo
```

## Tools
| Tool | Description |
|------|-------------|
| `code_run` | Execute Python/shell/JS snippet in sandbox |
| `code_run_sql` | Execute SQL against configured DB (read-only) |
| `code_session_list` | List active execution sessions |
| `code_session_kill` | Kill a running session |

## Security: 4 Layers
1. **Code validation** — size limit, blocklist patterns
2. **Process isolation** — separate process, timeout, resource caps
3. **Tenant isolation** — per-tenant working directory
4. **Result sanitization** — truncate output, strip ANSI
