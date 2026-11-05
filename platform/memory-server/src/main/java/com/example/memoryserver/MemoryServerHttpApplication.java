/**
 * Tenant-scoped memory and skill service.
 *
 * <p>Default runtime: Spring Boot on port 8180, authenticated REST tool bridge,
 * Spring AI MCP transport, H2 development storage, and optional Redis cache.</p>
 *
 * <p>Run with {@code JWT_SECRET=<32+ byte secret> mvn spring-boot:run} and check
 * readiness at {@code http://localhost:8180/actuator/health}.</p>
 */
package com.example.memoryserver;

import com.example.memorystore.config.MemoryStoreAutoConfig;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Import;

@SpringBootApplication(scanBasePackages = {"com.example.memoryserver", "com.example.mcp.common"})
@Import(MemoryStoreAutoConfig.class)
public class MemoryServerHttpApplication {

    public static void main(String[] args) {
        SpringApplication.run(MemoryServerHttpApplication.class, args);
    }
}
