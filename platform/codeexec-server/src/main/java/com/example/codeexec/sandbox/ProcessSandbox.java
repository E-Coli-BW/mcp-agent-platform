package com.example.codeexec.sandbox;

import com.example.codeexec.model.ExecutionResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Process-based code execution sandbox.
 *
 * Security layers:
 * 1. Code validation — size limit, language whitelist
 * 2. Per-tenant working directory — isolated filesystem
 * 3. Process timeout — killed after configurable deadline
 * 4. Output truncation — capped at maxOutputBytes
 *
 * MVP: subprocess with timeout. Production: Docker container per tenant.
 */
@Component
@Profile("dev")
@ConditionalOnProperty(name = "codeexec.sandbox.mode", havingValue = "process")
public class ProcessSandbox implements CodeSandbox {

    private static final Logger log = LoggerFactory.getLogger(ProcessSandbox.class);

    private final int timeoutSeconds;
    private final int maxCodeSize;
    private final int maxOutputBytes;
    private final Path workDir;
    private final List<String> allowedLanguages;

    private static final Map<String, List<String>> LANGUAGE_COMMANDS = Map.of(
            "python", List.of("python3", "-c"),
            "shell", List.of("sh", "-c"),
            "javascript", List.of("node", "-e")
    );

    public ProcessSandbox(
            @Value("${codeexec.timeout-seconds:30}") int timeoutSeconds,
            @Value("${codeexec.max-code-size-bytes:65536}") int maxCodeSize,
            @Value("${codeexec.max-output-bytes:10240}") int maxOutputBytes,
            @Value("${codeexec.work-dir:/tmp/codeexec}") String workDir,
            @Value("${codeexec.allowed-languages:python,shell,javascript}") List<String> allowedLanguages) {
        this.timeoutSeconds = timeoutSeconds;
        this.maxCodeSize = maxCodeSize;
        this.maxOutputBytes = maxOutputBytes;
        this.workDir = Path.of(workDir);
        this.allowedLanguages = allowedLanguages;
        log.warn("⚠️ ProcessSandbox is active — DEV ONLY. Do NOT use in production. "
                + "Set codeexec.sandbox.mode=docker for real isolation.");
    }

    static String sanitizeTenantId(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            throw new SecurityException("Tenant ID must not be null or blank");
        }

        String sanitized = java.util.Arrays.stream(tenantId.replace('\\', '/').split("/+"))
                .filter(segment -> !segment.isBlank())
                .filter(segment -> !segment.matches("^\\.+$"))
                .map(segment -> segment.replaceAll("[^A-Za-z0-9._-]", "_"))
                .filter(segment -> !segment.isBlank())
                .reduce((left, right) -> left + "_" + right)
                .orElseThrow(() -> new SecurityException("Tenant ID must not be only dots: " + tenantId));

        if (sanitized.matches("^\\.+$")) {
            throw new SecurityException("Tenant ID must not be only dots: " + tenantId);
        }
        return sanitized;
    }

    /**
     * Execute code in a sandboxed subprocess.
     */
    public ExecutionResult execute(String tenantId, String code, String language, Integer timeout) {
        // ── Validation ───────────────────────────────────────────
        String lang = language != null ? language.toLowerCase() : "python";

        if (!allowedLanguages.contains(lang)) {
            return ExecutionResult.error("Language '" + lang + "' not allowed. Allowed: " + allowedLanguages, lang);
        }

        if (code == null || code.isBlank()) {
            return ExecutionResult.error("Code cannot be empty", lang);
        }

        if (code.getBytes().length > maxCodeSize) {
            return ExecutionResult.error("Code too large (" + code.getBytes().length + " bytes). Max: " + maxCodeSize, lang);
        }

        List<String> command = LANGUAGE_COMMANDS.get(lang);
        if (command == null) {
            return ExecutionResult.error("No executor configured for language: " + lang, lang);
        }

        // ── Prepare working directory ────────────────────────────
        Path tenantDir;
        try {
            tenantDir = workDir.resolve(sanitizeTenantId(tenantId));
            Files.createDirectories(tenantDir);
        } catch (SecurityException e) {
            return ExecutionResult.error(e.getMessage(), lang);
        } catch (IOException e) {
            return ExecutionResult.error("Failed to create working directory: " + e.getMessage(), lang);
        }

        // ── Execute ──────────────────────────────────────────────
        int effectiveTimeout = Math.min(timeout != null ? timeout : timeoutSeconds, timeoutSeconds);
        long startTime = System.currentTimeMillis();

        try {
            var cmd = new java.util.ArrayList<>(command);
            cmd.add(code);

            ProcessBuilder pb = new ProcessBuilder(cmd)
                    .directory(tenantDir.toFile())
                    .redirectErrorStream(false);

            // Minimal environment — no inherited secrets
            pb.environment().clear();
            pb.environment().put("PATH", "/usr/local/bin:/usr/bin:/bin");
            pb.environment().put("HOME", tenantDir.toString());
            pb.environment().put("LANG", "en_US.UTF-8");

            Process process = pb.start();

            // Read streams in background threads (otherwise readStream blocks until process dies)
            var stdoutFuture = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                try { return readStream(process.getInputStream(), maxOutputBytes); }
                catch (IOException e) { return "[error reading stdout]"; }
            });
            var stderrFuture = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                try { return readStream(process.getErrorStream(), maxOutputBytes); }
                catch (IOException e) { return "[error reading stderr]"; }
            });

            boolean finished = process.waitFor(effectiveTimeout, TimeUnit.SECONDS);
            long durationMs = System.currentTimeMillis() - startTime;

            if (!finished) {
                process.destroyForcibly();
                process.waitFor(2, TimeUnit.SECONDS);
                String partialOut = stdoutFuture.getNow("");
                return ExecutionResult.timeout(partialOut, durationMs, lang);
            }

            String stdout = stdoutFuture.get(2, TimeUnit.SECONDS);
            String stderr = stderrFuture.get(2, TimeUnit.SECONDS);

            return ExecutionResult.success(stdout, stderr, process.exitValue(), durationMs, lang);

        } catch (IOException e) {
            log.error("Execution failed for tenant={}, language={}", tenantId, lang, e);
            return ExecutionResult.error("Execution failed: " + e.getMessage(), lang);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ExecutionResult.error("Execution interrupted", lang);
        } catch (Exception e) {
            return ExecutionResult.error("Unexpected error: " + e.getMessage(), lang);
        }
    }

    private String readStream(InputStream is, int maxBytes) throws IOException {
        try (var reader = new BufferedReader(new InputStreamReader(is))) {
            var sb = new StringBuilder();
            char[] buf = new char[1024];
            int read;
            while ((read = reader.read(buf)) != -1 && sb.length() < maxBytes) {
                sb.append(buf, 0, Math.min(read, maxBytes - sb.length()));
            }
            if (sb.length() >= maxBytes) {
                sb.append("\n[... output truncated at ").append(maxBytes).append(" bytes]");
            }
            return sb.toString();
        }
    }
}
