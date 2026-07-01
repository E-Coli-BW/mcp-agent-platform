package com.example.filesearch.sandbox;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Unit tests for PathSandbox — the critical security component.
 */
class PathSandboxTest {

    @TempDir
    Path tempDir;

    private PathSandbox sandbox;

    @BeforeEach
    void setUp() throws IOException {
        sandbox = new PathSandbox(tempDir.toRealPath().toString());
    }

    @Test
    void resolve_withinSandbox_succeeds() throws IOException {
        Files.createFile(tempDir.resolve("file.txt"));
        Path result = sandbox.resolve("tenant1", tempDir.toRealPath().resolve("file.txt").toString());
        assertTrue(result.startsWith(tempDir.toRealPath()));
    }

    @Test
    void resolve_outsideSandbox_throws() {
        assertThrows(SecurityException.class,
                () -> sandbox.resolve("tenant1", "/etc/passwd"));
    }

    @Test
    void resolve_pathTraversal_throws() {
        assertThrows(SecurityException.class,
                () -> sandbox.resolve("tenant1", tempDir + "/../../etc/passwd"));
    }

    @Test
    void resolve_symlinkEscape_throws() throws IOException {
        Path outsideDirectory = Files.createTempDirectory(tempDir.toRealPath().getParent(), "outside-");
        Path outsideFile = Files.writeString(outsideDirectory.resolve("outside.txt"), "outside");
        Path symlink = tempDir.resolve("escape-link");

        Files.createSymbolicLink(symlink, outsideFile);

        assertThrows(SecurityException.class,
                () -> sandbox.resolve("tenant1", symlink.toString()));
    }

    @Test
    void resolve_relativePathWithinSandbox_succeeds() throws IOException {
        Files.createFile(tempDir.resolve("test.txt"));
        Path result = sandbox.resolve("tenant1", tempDir.toRealPath().resolve("./test.txt").toString());
        assertTrue(result.startsWith(tempDir.toRealPath()));
    }

    @Test
    void isAllowed_returnsTrueForSafePaths() throws IOException {
        Path safeFile = Files.writeString(tempDir.resolve("safe.txt"), "safe");

        assertTrue(sandbox.isAllowed("tenant1", safeFile.toRealPath().toString()));
    }

    @Test
    void isAllowed_returnsFalseForUnsafePaths() {
        assertFalse(sandbox.isAllowed("tenant1", "/etc/shadow"));
    }

    @Test
    void getRoot_returnsDefaultRoot() throws IOException {
        assertEquals(tempDir.toRealPath(), sandbox.getRoot("any-tenant"));
    }

    @Test
    void should_allowMissingFileWithinSandbox_when_parentExists() throws IOException {
        // Non-existent leaves must be allowed (so the agent can create new files,
        // and `read missing.txt` still returns a friendly "not found" rather than
        // a 500). The parent-walk in resolveRaw canonicalises the existing parent
        // so symlink escapes are still blocked.
        Path missingPath = tempDir.toRealPath().resolve("missing.txt");

        Path resolved = sandbox.resolve("tenant1", missingPath.toString());

        assertEquals(missingPath, resolved);
    }

    @Test
    void should_rejectMissingFileViaSymlinkEscape_when_parentLinksOutside() throws IOException {
        // The dangerous case: a missing leaf under a symlinked directory that
        // points outside the sandbox. The parent-walk must follow the symlink
        // on the existing ancestor and detect the escape.
        Path outsideDir = Files.createTempDirectory(tempDir.toRealPath().getParent(), "outside-");
        Path symlinkDir = tempDir.resolve("escape-dir");
        Files.createSymbolicLink(symlinkDir, outsideDir);

        String missingViaSymlink = symlinkDir.resolve("not-yet-created.txt").toString();

        assertThrows(SecurityException.class,
                () -> sandbox.resolve("tenant1", missingViaSymlink));
    }

    @Test
    void should_rejectMissingFileOutsideSandbox_when_pathIsAbsolute() {
        // A non-existent file outside the sandbox must still be rejected.
        assertThrows(SecurityException.class,
                () -> sandbox.resolve("tenant1", "/var/log/does-not-exist.log"));
    }
}
