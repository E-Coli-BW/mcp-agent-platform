# Sentinel + Nacos + Feign/Dubbo Migration & Design Doc

## Goals
- Move to a true distributed, cloud-native architecture (Kubernetes-ready).
- Use Sentinel for resilience, Nacos for service discovery/config, Feign/Dubbo for RPC.
- Retain “all-in-one”/monolith mode as fallback via SPI/pluggable interfaces.

---

## Migration Checklist

### Service Discovery & Registration
- [ ] Integrate Nacos SDK in all services (Java/Python).
- [ ] Register each service with Nacos at startup.
- [ ] Refactor service clients to resolve endpoints via Nacos, not static config.

### Configuration Management
- [ ] Move config (DB, Redis, Kafka, etc.) to Nacos.
- [ ] Refactor config loading to use Nacos SDK (hot-reload if possible).

### Resilience Layer
- [ ] Replace Resilience4j with Sentinel in Java services.
- [ ] Integrate Sentinel Python SDK or use sidecar/proxy for Python.
- [ ] Add Sentinel dashboard for monitoring.

### RPC/Communication
- [ ] Refactor inter-service calls to use Feign (HTTP/gRPC) or Dubbo.
- [ ] Integrate Sentinel and Nacos with Feign/Dubbo clients.
- [ ] Keep REST endpoints for fallback/local mode.

### SPI/Pluggable Architecture
- [ ] Define SPI interfaces for:
  - Service discovery
  - Config management
  - RPC client
  - Resilience
- [ ] Implement “local” (static config, REST, Resilience4j) and “cloud-native” (Nacos, Feign/Dubbo, Sentinel) providers.
- [ ] Add config/env to select provider at runtime.

### Kubernetes Readiness
- [ ] Write K8s manifests/Helm charts for all services.
- [ ] Ensure Nacos, Sentinel, and Dubbo/Feign are K8s-ready.
- [ ] Add health checks and readiness probes.

### Fallback/Compatibility
- [ ] Ensure “same instance”/monolith mode works for local/dev.
- [ ] Document how to switch between modes.

---

## Design Notes

- Use SPI (Java) or plugin/entry points (Python) to select between local and distributed providers at runtime.
- All new service clients should depend on interfaces, not concrete implementations.
- Document and test both modes (local/dev and distributed/K8s).

---

## SPI Code Sample (Java)

```java
// SPI Interface for Service Discovery
public interface ServiceDiscovery {
    String resolve(String serviceName);
}

// Nacos Implementation
public class NacosDiscovery implements ServiceDiscovery {
    @Override
    public String resolve(String serviceName) {
        // Use Nacos SDK to resolve service
        return nacosClient.getService(serviceName);
    }
}

// Static (local) Implementation
public class StaticDiscovery implements ServiceDiscovery {
    private final Map<String, String> staticMap;
    public StaticDiscovery(Map<String, String> staticMap) {
        this.staticMap = staticMap;
    }
    @Override
    public String resolve(String serviceName) {
        return staticMap.get(serviceName);
    }
}

// SPI Loader
public class DiscoveryProvider {
    public static ServiceDiscovery load() {
        String mode = System.getenv("DISCOVERY_MODE");
        if ("nacos".equalsIgnoreCase(mode)) {
            return new NacosDiscovery();
        } else {
            // fallback to static config
            return new StaticDiscovery(Map.of(
                "memory-server", "http://localhost:8081",
                "filesearch-server", "http://localhost:8082"
            ));
        }
    }
}
```

---

## RPC Spec Definition Sample (Feign, Dubbo, gRPC)

### Feign (Java, HTTP)
```java
@FeignClient(name = "memory-server", url = "${memory.server.url}")
public interface MemoryServerClient {
    @GetMapping("/memory/{key}")
    MemoryEntry getMemory(@PathVariable("key") String key);

    @PostMapping("/memory")
    void setMemory(@RequestBody MemoryEntry entry);
}
```

### Dubbo (Java, RPC)
```java
public interface MemoryService {
    MemoryEntry getMemory(String key);
    void setMemory(MemoryEntry entry);
}

// Dubbo annotation for provider
@Service
public class MemoryServiceImpl implements MemoryService { ... }
```

### gRPC (proto)
```proto
syntax = "proto3";

service MemoryService {
  rpc GetMemory (MemoryRequest) returns (MemoryEntry);
  rpc SetMemory (MemoryEntry) returns (Empty);
}

message MemoryRequest {
  string key = 1;
}
message MemoryEntry {
  string key = 1;
  string value = 2;
}
message Empty {}
```

---

## How to Switch Modes
- Use an environment variable or config file to select between local and distributed providers at startup.
- Document the process in README or deployment docs.

---

## References
- Sentinel: https://github.com/alibaba/Sentinel
- Nacos: https://nacos.io/en-us/
- Dubbo: https://dubbo.apache.org/en/
- Feign: https://spring.io/projects/spring-cloud-openfeign
- gRPC: https://grpc.io/
