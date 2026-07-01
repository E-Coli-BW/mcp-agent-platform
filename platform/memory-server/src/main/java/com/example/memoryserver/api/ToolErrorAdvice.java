package com.example.memoryserver.api;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ControllerAdvice;
import org.springframework.web.bind.annotation.ExceptionHandler;

import java.util.Map;

/**
 * Translates uncaught exceptions into MCP-compatible error responses.
 * Runs after the circuit breaker records the failure.
 */
@ControllerAdvice(assignableTypes = ToolBridgeController.class)
public class ToolErrorAdvice {

    private static final Logger log = LoggerFactory.getLogger(ToolErrorAdvice.class);

    @ExceptionHandler(Throwable.class)
    public ResponseEntity<Map<String, String>> handle(Throwable t) {
        log.error("Tool bridge error", t);
        return ResponseEntity.status(500)
                .body(Map.of("result", "❌ Service error: " + t.getMessage()));
    }
}
