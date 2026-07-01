package com.example.auth;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * Auth Service entry point.
 *
 * <p>Why the explicit {@link EnableJpaRepositories} basePackages?
 * Spring Data Redis is on the classpath (for caching + rate limiting) and
 * its repository scanner doesn't know the JPA interfaces in
 * {@code com.example.auth.repository} are JPA-only. The default behavior
 * is "candidate every repository for every Spring Data module" which prints
 * a stack of WARN lines like:
 *
 * <pre>
 *   Spring Data Redis - Could not safely identify store assignment for
 *   repository candidate interface AuthClientRepository; consider annotating...
 * </pre>
 *
 * Pinning {@code basePackages} on JPA tells the Redis scanner "those aren't
 * yours" and silences the noise. Boot still works without this, but the
 * WARN noise drowns out real signals during startup.
 */
@SpringBootApplication
@EnableScheduling
@EnableJpaRepositories(basePackages = "com.example.auth.repository")
public class AuthServiceApplication {
    public static void main(String[] args) {
        SpringApplication.run(AuthServiceApplication.class, args);
    }
}
