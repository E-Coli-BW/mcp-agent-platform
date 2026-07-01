package com.example.agent;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

/**
 * Entry point for the Java agent server.
 */
@SpringBootApplication
@ConfigurationPropertiesScan
public class AgentServerApplication {

    /**
     * Start the Spring Boot application.
     *
     * @param args command-line arguments
     */
    public static void main(String[] args) {
        SpringApplication.run(AgentServerApplication.class, args);
    }
}
