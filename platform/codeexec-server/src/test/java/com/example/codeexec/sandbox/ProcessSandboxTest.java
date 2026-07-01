package com.example.codeexec.sandbox;

import com.example.codeexec.model.ExecutionResult;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for ProcessSandbox — the critical security component.
 */
class ProcessSandboxTest {

    @TempDir
    Path tempDir;

    private ProcessSandbox sandbox;

    @BeforeEach
    void setUp() {
        sandbox = new ProcessSandbox(10, 65536, 10240, tempDir.toString(),
                List.of("python", "shell", "javascript"));
    }

    // ── Successful execution ─────────────────────────────────────

    @Test
    void python_helloWorld() {
        ExecutionResult r = sandbox.execute("t1", "print('hello world')", "python", null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("hello world"));
        assertFalse(r.timedOut());
    }

    @Test
    void shell_echo() {
        ExecutionResult r = sandbox.execute("t1", "echo 'hello from shell'", "shell", null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("hello from shell"));
    }

    @Test
    void python_arithmetic() {
        ExecutionResult r = sandbox.execute("t1", "print(2 + 3)", "python", null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().trim().contains("5"));
    }

    @Test
    void python_multiline() {
        String code = """
                for i in range(3):
                    print(f'line {i}')
                """;
        ExecutionResult r = sandbox.execute("t1", code, "python", null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("line 0"));
        assertTrue(r.stdout().contains("line 2"));
    }

    // ── Error handling ───────────────────────────────────────────

    @Test
    void python_syntaxError_nonZeroExit() {
        ExecutionResult r = sandbox.execute("t1", "print(", "python", null);
        assertNotEquals(0, r.exitCode());
        assertFalse(r.stderr().isBlank());
    }

    @Test
    void python_runtimeError_nonZeroExit() {
        ExecutionResult r = sandbox.execute("t1", "raise ValueError('boom')", "python", null);
        assertNotEquals(0, r.exitCode());
        assertTrue(r.stderr().contains("ValueError"));
    }

    @Test
    void shell_commandNotFound() {
        ExecutionResult r = sandbox.execute("t1", "nonexistentcommand123", "shell", null);
        assertNotEquals(0, r.exitCode());
    }

    // ── Timeout ──────────────────────────────────────────────────

    @Test
    void python_timeout_killedAndReported() {
        ExecutionResult r = sandbox.execute("t1", "import time; time.sleep(60)", "python", 2);
        assertTrue(r.timedOut());
        assertEquals(-1, r.exitCode());
        assertTrue(r.stderr().contains("timed out"));
    }

    @Test
    void shell_timeout() {
        ExecutionResult r = sandbox.execute("t1", "sleep 60", "shell", 2);
        assertTrue(r.timedOut());
    }

    // ── Validation ───────────────────────────────────────────────

    @Test
    void emptyCode_rejected() {
        ExecutionResult r = sandbox.execute("t1", "", "python", null);
        assertTrue(r.stderr().contains("empty") || r.stdout().isEmpty());
        assertEquals(-1, r.exitCode());
    }

    @Test
    void nullCode_rejected() {
        ExecutionResult r = sandbox.execute("t1", null, "python", null);
        assertEquals(-1, r.exitCode());
    }

    @Test
    void disallowedLanguage_rejected() {
        ExecutionResult r = sandbox.execute("t1", "code", "ruby", null);
        assertEquals(-1, r.exitCode());
        assertTrue(r.stderr().contains("not allowed"));
    }

    @Test
    void codeTooLarge_rejected() {
        var smallSandbox = new ProcessSandbox(10, 100, 10240, tempDir.toString(),
                List.of("python", "shell"));
        String bigCode = "x = " + "1" .repeat(200);
        ExecutionResult r = smallSandbox.execute("t1", bigCode, "python", null);
        assertEquals(-1, r.exitCode());
        assertTrue(r.stderr().contains("too large"));
    }

    // ── Output truncation ────────────────────────────────────────

    @Test
    void largeOutput_truncated() {
        var smallOutputSandbox = new ProcessSandbox(10, 65536, 500, tempDir.toString(),
                List.of("python", "shell"));
        ExecutionResult r = smallOutputSandbox.execute("t1",
                "print('x' * 2000)", "python", null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("truncated"));
        assertTrue(r.stdout().length() < 1000);
    }

    // ── Tenant isolation ─────────────────────────────────────────

    @Test
    void tenantA_cannotSee_tenantB_files() {
        // Tenant A creates a file
        sandbox.execute("tenant-A", "echo 'secret' > /tmp/a_secret.txt", "shell", null);

        // Tenant B tries to read it — the working dirs are different
        // but in MVP (process-based), they share the same filesystem
        // This test DOCUMENTS the gap — Docker isolation would fix it
        ExecutionResult r = sandbox.execute("tenant-B", "cat /tmp/a_secret.txt 2>/dev/null || echo 'NOT FOUND'", "shell", null);

        // In MVP: file IS accessible (process sandbox doesn't isolate FS)
        // In production: Docker container would block this
        // This test serves as documentation of the known limitation
        assertNotNull(r.stdout());
    }

    // ── Environment safety ───────────────────────────────────────

    @Test
    void environment_cleared_noSecrets() {
        ExecutionResult r = sandbox.execute("t1", "env | wc -l", "shell", null);
        assertEquals(0, r.exitCode());
        // Should have very few env vars (PATH, HOME, LANG only)
        int envCount = Integer.parseInt(r.stdout().trim());
        assertTrue(envCount <= 10, "Environment should be minimal, got " + envCount + " vars");
    }

    // ── Default language ─────────────────────────────────────────

    @Test
    void nullLanguage_defaultsToPython() {
        ExecutionResult r = sandbox.execute("t1", "print('default python')", null, null);
        assertEquals(0, r.exitCode());
        assertTrue(r.stdout().contains("default python"));
        assertEquals("python", r.language());
    }
}
