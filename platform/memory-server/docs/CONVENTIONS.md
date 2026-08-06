# Coding Conventions & Agent Instructions

This file ensures consistent code style across all iterations, whether written by human or AI.

## Java Style

### General
- Java 21, use records where immutable data is needed
- Spring Boot 3.4 conventions
- Constructor injection, never field injection
- `final` on all injected dependencies
- No Lombok in MVP (explicit code > magic)

### Naming
- Entity: `MemoryEntity` (JPA)
- DTO: `MemoryRequest`, `MemoryResponse` (API boundary)
- Repository: `MemoryRepository` (Spring Data)
- Service: `MemoryService` (business logic)
- Tool: `MemoryToolService` (MCP tool definitions)

### Error Handling
- Business exceptions extend `RuntimeException` with meaningful messages
- Never return null from service methods, use `Optional`
- Tool methods return String (MCP protocol requirement), never throw

### Multi-tenancy
- `tenantId` is ALWAYS the first parameter in service/repository methods
- Never trust client-provided tenantId, always extract from JWT
- Every DB query MUST include `tenant_id` in WHERE clause

### Testing
- Unit tests: `@ExtendWith(MockitoExtension.class)`, mock repository/cache
- Integration tests: `@SpringBootTest` with Testcontainers
- Test method naming: `should_doSomething_when_condition()`

## Documentation

### Each module design doc must include:
1. **What** — one sentence description
2. **Why** — why this module exists, what problem it solves
3. **Interface** — public methods / API contract
4. **Tradeoffs** — what was considered and why chosen
5. **Future** — what would change at scale

### Code comments
- English for all code identifiers and comments
- Chinese OK in design docs (.md) for architecture discussions
- Javadoc on all public methods
- Inline comments only for non-obvious logic

## MCP Tool Convention

All tool methods must:
1. Accept primitive types or simple objects (MCP limitation)
2. Return `String` (MCP text content)
3. Handle all errors internally, return error message string
4. Include emoji prefix in responses (✅❌🔍📌🗑️📋)
5. Log to audit trail before returning

## Git Commit Convention

```
feat(module): short description
fix(module): what was fixed
docs(module): documentation update
refactor(module): no behavior change
test(module): test additions
```
