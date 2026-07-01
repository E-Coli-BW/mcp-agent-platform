package com.example.filesearch;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"com.example.filesearch", "com.example.mcp.common"})
public class FileSearchApplication {
    public static void main(String[] args) {
        SpringApplication.run(FileSearchApplication.class, args);
    }
}
