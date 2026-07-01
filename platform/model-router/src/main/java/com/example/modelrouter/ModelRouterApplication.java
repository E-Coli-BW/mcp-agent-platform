package com.example.modelrouter;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"com.example.modelrouter", "com.example.mcp.common"})
public class ModelRouterApplication {
    public static void main(String[] args) {
        SpringApplication.run(ModelRouterApplication.class, args);
    }
}
