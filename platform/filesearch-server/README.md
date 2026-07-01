# File Search MCP Server

Enterprise-grade file system search and browsing MCP server with Spring Boot 3.4 + Java 21.

## Quick Start

```bash
# Build
command mvn clean install -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo -DskipTests

# Run
command mvn spring-boot:run -f pom.xml \
  -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo

# Get a dev token
curl 'http://localhost:8280/dev/token?tenant=test'
```

## Tools
| Tool | Description |
|------|-------------|
| `file_search` | Grep/ripgrep across directory tree |
| `file_read` | Read a file (or line range) |
| `file_list` | List directory contents |
| `file_tree` | Tree view of directory structure |
| `file_stat` | File metadata (size, modified, type) |
| `file_glob` | Find files matching glob pattern |

## Architecture
- Stateless (read-only filesystem access)
- Path sandboxing per tenant (prevent traversal attacks)
- Token-aware result truncation
- Ripgrep for fast search
