/**
 * Memory Store MCP Server — Java HTTP (long-running) version
 *
 * Key differences from stdio version:
 *   1. HTTP/SSE transport — multiple clients connect to one server
 *   2. Spring Boot — auto-config, actuator, metrics out of the box
 *   3. ConcurrentHashMap — thread-safe for parallel requests
 *   4. JVM warms up — after first request, latency drops to <5ms
 *   5. Can add Spring Security, rate limiting, audit logging easily
 *
 * Run:   mvn spring-boot:run
 * Test:  curl http://localhost:8080/sse
 *
 * Client config (VS Code mcp.json):
 *   { "type": "sse", "url": "http://localhost:8080/sse" }
 */
package com.example.memoryserver;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"com.example.memoryserver", "com.example.mcp.common"})
public class MemoryServerHttpApplication {

    public static void main(String[] args) {
        SpringApplication.run(MemoryServerHttpApplication.class, args);
    }
}
