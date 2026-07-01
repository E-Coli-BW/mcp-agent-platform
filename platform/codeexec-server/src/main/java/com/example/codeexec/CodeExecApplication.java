package com.example.codeexec;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"com.example.codeexec", "com.example.mcp.common"})
public class CodeExecApplication {
    public static void main(String[] args) {
        SpringApplication.run(CodeExecApplication.class, args);
    }
}
