package com.example.codeexec.sandbox;

import com.example.codeexec.model.ExecutionResult;

/**
 * Code execution sandbox interface.
 * Implementations: ProcessSandbox (dev), DockerSandbox (production).
 * Switch via: codeexec.sandbox.mode=process|docker
 */
public interface CodeSandbox {
    ExecutionResult execute(String tenantId, String code, String language, Integer timeout);
}
