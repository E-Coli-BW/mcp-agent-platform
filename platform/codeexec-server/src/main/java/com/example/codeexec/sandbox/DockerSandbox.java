package com.example.codeexec.sandbox;

import com.example.codeexec.model.ExecutionResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Docker-based code execution sandbox — true process isolation.
 *
 * Each execution runs in a disposable Docker container hardened against the
 * common container-escape / resource-exhaustion vectors (OWASP container
 * hardening checklist):
 * - --rm                          — container removed after execution
 * - --network=none                — no network access (can't exfiltrate data)
 * - --read-only                   — no filesystem writes outside /tmp
 * - --tmpfs /tmp:size=64m         — small writable scratch space
 * - --memory=256m                 — OOM killed if exceeds 256MB
 * - --cpus=0.5                    — max half a CPU core
 * - --pids-limit=64               — no fork bombs (cgroup level)
 * - --user=nobody                 — unprivileged user
 * - --security-opt no-new-privileges — block setuid privilege escalation
 * - --cap-drop=ALL                — drop all Linux capabilities (Docker keeps 14 by default)
 * - --ipc=none                    — no shared-memory side channels
 * - --ulimit nofile=64:64         — file-descriptor ceiling
 * - --ulimit nproc=32:32          — process ceiling (ulimit fires before the cgroup pids-limit)
 *
 * The user's code is streamed in via the container's <b>stdin</b> (the
 * interpreter is launched in stdin-reading mode, e.g. {@code python3 -}), never
 * passed as a {@code docker run ... -c <code>} argument. This keeps the code out
 * of the host's process table (`ps -ef`) and the Linux audit log, so secrets a
 * model might emit into code can't leak through process listings.
 *
 * Activated by: codeexec.sandbox.mode=docker (default: docker)
 * Requires: Docker daemon accessible by the server process.
 */
@Component
@ConditionalOnProperty(name = "codeexec.sandbox.mode", havingValue = "docker", matchIfMissing = true)
public class DockerSandbox implements CodeSandbox {

    private static final Logger log = LoggerFactory.getLogger(DockerSandbox.class);

    private final int timeoutSeconds;
    private final int maxCodeSize;
    private final int maxOutputBytes;
    private final String memoryLimit;
    private final String cpuLimit;
    private final List<String> allowedLanguages;

    private static final Map<String, String> LANGUAGE_IMAGES = Map.of(
            "python", "python:3.12-alpine",
            "javascript", "node:20-alpine",
            "shell", "alpine:3.19"
    );

    /**
     * Interpreter invocation per language. Each reads the program from
     * {@code /dev/stdin} rather than an inline {@code -c <code>} argument, so the
     * user's code never appears in the host process table or audit log.
     */
    private static final Map<String, List<String>> LANGUAGE_CMD_PREFIX = Map.of(
            "python", List.of("python3", "/dev/stdin"),
            "javascript", List.of("node", "/dev/stdin"),
            "shell", List.of("sh", "/dev/stdin")
    );

    /** Allowed shape for a tenant id used in a docker --label (defense-in-depth). */
    private static final java.util.regex.Pattern TENANT_ID_PATTERN =
            java.util.regex.Pattern.compile("^[A-Za-z0-9_-]{1,64}$");

    public DockerSandbox(
            @Value("${codeexec.timeout-seconds:30}") int timeoutSeconds,
            @Value("${codeexec.max-code-size-bytes:65536}") int maxCodeSize,
            @Value("${codeexec.max-output-bytes:10240}") int maxOutputBytes,
            @Value("${codeexec.docker.memory-limit:256m}") String memoryLimit,
            @Value("${codeexec.docker.cpu-limit:0.5}") String cpuLimit,
            @Value("${codeexec.allowed-languages:python,shell,javascript}") List<String> allowedLanguages) {
        this.timeoutSeconds = timeoutSeconds;
        this.maxCodeSize = maxCodeSize;
        this.maxOutputBytes = maxOutputBytes;
        this.memoryLimit = memoryLimit;
        this.cpuLimit = cpuLimit;
        this.allowedLanguages = allowedLanguages;
        log.info("🐳 DockerSandbox initialized (memory={}, cpu={}, timeout={}s)",
                memoryLimit, cpuLimit, timeoutSeconds);
        prePullImagesAsync();
    }

    /**
     * Pre-pull images in the background so a cold image cache doesn't block
     * application startup. Pulling 3 images synchronously could take minutes;
     * a K8s liveness probe (≈30s) would kill the pod before it ever became
     * ready, causing an infinite restart loop. The first execution of an
     * un-pulled language simply pays a one-time pull cost instead.
     */
    private void prePullImagesAsync() {
        Thread t = new Thread(this::prePullImages, "docker-image-prepull");
        t.setDaemon(true);   // never hold up JVM shutdown
        t.start();
    }

    /**
     * Pre-pull Docker images so the first execution doesn't spend 30+ seconds
     * downloading (which looks like a timeout to the user).
     */
    private void prePullImages() {
        for (var entry : LANGUAGE_IMAGES.entrySet()) {
            String lang = entry.getKey();
            String image = entry.getValue();
            try {
                var check = new ProcessBuilder("docker", "image", "inspect", image)
                        .redirectErrorStream(true).start();
                if (check.waitFor(5, java.util.concurrent.TimeUnit.SECONDS) && check.exitValue() == 0) {
                    log.info("  ✓ Image ready: {} ({})", lang, image);
                } else {
                    log.info("  ⏳ Pulling image: {} ({})...", lang, image);
                    new ProcessBuilder("docker", "pull", image)
                            .inheritIO().start().waitFor(120, java.util.concurrent.TimeUnit.SECONDS);
                    log.info("  ✓ Pulled: {}", image);
                }
            } catch (Exception e) {
                log.warn("  ⚠️ Cannot pre-pull {} ({}): {}. First execution may be slow.",
                        lang, image, e.getMessage());
            }
        }
    }

    public ExecutionResult execute(String tenantId, String code, String language, Integer timeout) {
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

        String image = LANGUAGE_IMAGES.get(lang);
        List<String> cmdPrefix = LANGUAGE_CMD_PREFIX.get(lang);
        if (image == null || cmdPrefix == null) {
            return ExecutionResult.error("No Docker image for language: " + lang, lang);
        }

        int effectiveTimeout = Math.min(timeout != null ? timeout : timeoutSeconds, timeoutSeconds);
        long startTime = System.currentTimeMillis();

        try {
            List<String> cmd = buildDockerCommand(tenantId, image, cmdPrefix);

            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.environment().clear();
            pb.environment().put("PATH", "/usr/local/bin:/usr/bin:/bin");

            Process process = pb.start();

            // Stream the user's code in via stdin, then close it so the
            // interpreter reading /dev/stdin sees EOF and starts executing.
            // Code is NEVER passed as a docker argument → invisible to `ps`/audit log.
            try (var stdin = process.getOutputStream()) {
                stdin.write(code.getBytes(StandardCharsets.UTF_8));
            } catch (Exception e) {
                log.warn("Failed to stream code to sandbox stdin for tenant={}: {}", tenantId, e.getMessage());
            }

            var stdoutFuture = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                try { return readStream(process.getInputStream()); }
                catch (Exception e) { return "[error reading stdout]"; }
            });
            var stderrFuture = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                try { return readStream(process.getErrorStream()); }
                catch (Exception e) { return "[error reading stderr]"; }
            });

            boolean finished = process.waitFor(effectiveTimeout, TimeUnit.SECONDS);
            long durationMs = System.currentTimeMillis() - startTime;

            if (!finished) {
                process.destroyForcibly();
                process.waitFor(2, TimeUnit.SECONDS);
                return ExecutionResult.timeout(stdoutFuture.getNow(""), durationMs, lang);
            }

            String stdout = stdoutFuture.get(2, TimeUnit.SECONDS);
            String stderr = stderrFuture.get(2, TimeUnit.SECONDS);
            return ExecutionResult.success(stdout, stderr, process.exitValue(), durationMs, lang);

        } catch (Exception e) {
            log.error("Docker execution failed for tenant={}, lang={}: {}", tenantId, lang, e.getMessage());
            return ExecutionResult.error("Docker execution failed: " + e.getMessage(), lang);
        }
    }

    /**
     * Build the {@code docker run} argument list for one execution.
     *
     * <p>Package-private and pure (no process launch) so the hardening flags can
     * be asserted in unit tests — a missing {@code --cap-drop=ALL} or
     * {@code no-new-privileges} should fail CI, not ship silently.
     *
     * <p>The interpreter is invoked to read from {@code /dev/stdin}; the code
     * itself is fed over stdin by the caller and is intentionally NOT part of
     * this argument list.
     *
     * @param tenantId  caller tenant (validated; falls back to "unknown" if malformed)
     * @param image     resolved container image
     * @param cmdPrefix interpreter invocation (e.g. {@code [python3, /dev/stdin]})
     */
    List<String> buildDockerCommand(String tenantId, String image, List<String> cmdPrefix) {
        // Defense-in-depth: tenantId reaches a shell-adjacent --label; never trust it raw.
        String safeTenant = (tenantId != null && TENANT_ID_PATTERN.matcher(tenantId).matches())
                ? tenantId : "unknown";

        List<String> cmd = new ArrayList<>(List.of(
                "docker", "run",
                "--rm",                                   // remove container after execution
                "--interactive",                          // keep stdin open so we can pipe code in
                "--network=none",                         // no network access
                "--read-only",                            // read-only root filesystem
                "--tmpfs", "/tmp:rw,size=64m",            // writable /tmp, 64MB max
                "--memory=" + memoryLimit,                // memory limit
                "--memory-swap=" + memoryLimit,           // disallow swap → real memory cap
                "--cpus=" + cpuLimit,                     // CPU limit
                "--pids-limit=64",                        // no fork bombs (cgroup)
                "--user=nobody",                          // unprivileged user
                "--security-opt", "no-new-privileges",    // block setuid privilege escalation
                "--cap-drop=ALL",                         // drop all Linux capabilities
                "--ipc=none",                             // no shared-memory side channels
                "--ulimit", "nofile=64:64",               // file-descriptor ceiling
                "--ulimit", "nproc=32:32",                // process ceiling (fires before cgroup pids-limit)
                "--label", "tenant=" + safeTenant,        // for monitoring/cleanup
                image
        ));
        cmd.addAll(cmdPrefix);
        return cmd;
    }

    private String readStream(java.io.InputStream is) throws Exception {
        var sb = new StringBuilder();
        try (var reader = new BufferedReader(new InputStreamReader(is))) {
            char[] buf = new char[1024];
            int read;
            while ((read = reader.read(buf)) != -1 && sb.length() < maxOutputBytes) {
                sb.append(buf, 0, Math.min(read, maxOutputBytes - sb.length()));
            }
        }
        if (sb.length() >= maxOutputBytes) {
            sb.append("\n[... output truncated at ").append(maxOutputBytes).append(" bytes]");
        }
        return sb.toString();
    }
}
