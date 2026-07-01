package com.example.codeexec.sandbox;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link DockerSandbox#buildDockerCommand} — the security-critical
 * part of the Docker sandbox.
 *
 * <p>These assert the OWASP container-hardening flags are present in the
 * {@code docker run} argument list. They run without a Docker daemon (pure
 * argument construction), so a regression — e.g. someone dropping
 * {@code --cap-drop=ALL} — fails CI instead of silently shipping an
 * under-hardened sandbox.
 */
class DockerSandboxHardeningTest {

    private DockerSandbox newSandbox() {
        // allowedLanguages kept minimal; constructor kicks off a background
        // (daemon) image pre-pull that no-ops fast when Docker is absent.
        return new DockerSandbox(30, 65536, 10240, "256m", "0.5",
                List.of("python", "shell", "javascript"));
    }

    private List<String> cmd() {
        return newSandbox().buildDockerCommand(
                "tenant-a", "python:3.12-alpine", List.of("python3", "/dev/stdin"));
    }

    @Test
    void dropsAllCapabilities() {
        assertTrue(cmd().contains("--cap-drop=ALL"),
                "must drop all Linux capabilities");
    }

    @Test
    void blocksPrivilegeEscalation() {
        List<String> c = cmd();
        int i = c.indexOf("--security-opt");
        assertTrue(i >= 0 && i + 1 < c.size() && c.get(i + 1).equals("no-new-privileges"),
                "must set --security-opt no-new-privileges");
    }

    @Test
    void disablesSharedMemoryIpc() {
        assertTrue(cmd().contains("--ipc=none"),
                "must isolate IPC namespace to block shared-memory side channels");
    }

    @Test
    void hasFileAndProcessUlimits() {
        List<String> c = cmd();
        assertTrue(c.contains("nofile=64:64"), "must cap file descriptors");
        assertTrue(c.contains("nproc=32:32"), "must cap process count");
    }

    @Test
    void runsUnprivilegedAndNetworkless() {
        List<String> c = cmd();
        assertTrue(c.contains("--user=nobody"), "must run as unprivileged user");
        assertTrue(c.contains("--network=none"), "must disable networking");
        assertTrue(c.contains("--read-only"), "root filesystem must be read-only");
    }

    @Test
    void disablesSwapSoMemoryCapIsReal() {
        List<String> c = cmd();
        assertTrue(c.contains("--memory=256m"), "must cap memory");
        assertTrue(c.contains("--memory-swap=256m"),
                "memory-swap must equal memory so the container can't escape the cap via swap");
    }

    @Test
    void interpreterReadsFromStdinNotInlineArg() {
        // /dev/stdin must be present; the literal user code must NOT be an argument
        // (that would leak it to `ps`/audit log). We only pass the interpreter prefix.
        List<String> c = cmd();
        assertTrue(c.contains("/dev/stdin"), "interpreter should read code from stdin");
        assertEquals("/dev/stdin", c.get(c.size() - 1),
                "stdin path should be the final arg; raw code must not be appended");
    }

    @Test
    void keepsStdinOpenForPiping() {
        assertTrue(cmd().contains("--interactive"),
                "must keep stdin open (-i) so code can be piped in");
    }

    @Test
    void malformedTenantIdIsNotPassedThrough() {
        // A tenant id that could break out of the --label value falls back to "unknown".
        List<String> c = newSandbox().buildDockerCommand(
                "evil\",tenant", "python:3.12-alpine", List.of("python3", "/dev/stdin"));
        assertTrue(c.contains("tenant=unknown"),
                "malformed tenant id must be sanitized to a safe placeholder");
        assertFalse(c.stream().anyMatch(s -> s.contains("evil")),
                "raw malformed tenant id must not reach the docker command");
    }

    @Test
    void wellFormedTenantIdIsPreserved() {
        List<String> c = newSandbox().buildDockerCommand(
                "brand-audi_01", "python:3.12-alpine", List.of("python3", "/dev/stdin"));
        assertTrue(c.contains("tenant=brand-audi_01"),
                "a valid tenant id should be passed through unchanged");
    }
}
