package com.example.memorystore.spi.impl;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.stream.IntStream;

import static org.junit.jupiter.api.Assertions.*;

class FileMemoryBackendSecurityTest {

    @TempDir
    Path tempDirRoot;

    /**
     * The sandbox base directory the backend uses. We nest it one level
     * deeper than {@link TempDir} so that the test can safely probe
     * {@code baseDir/../escape} as an attacker-controlled path: the parent
     * is a fresh per-test directory ({@link TempDir}) that JUnit deletes,
     * so leftover artifacts from previous {@code mvn test} runs cannot
     * poison the {@code assertFalse(Files.exists(escapedFile))} checks.
     */
    Path baseDir;

    FileMemoryBackend backend;

    @BeforeEach
    void setUp() throws IOException {
        baseDir = Files.createDirectory(tempDirRoot.resolve("sandbox"));
        backend = new FileMemoryBackend(baseDir);
    }

    @Test
    void should_rejectTenantTraversal_when_tenantContainsDotDot() throws IOException {
        backend.save("../escape", "key", "data");

        Path safeFile = baseDir.resolve(".._escape").resolve("key.json");
        Path escapedFile = baseDir.resolve("../escape/key.json").normalize();

        assertTrue(Files.exists(safeFile));
        assertEquals("data", Files.readString(safeFile));
        assertFalse(Files.exists(escapedFile),
                "Traversal target " + escapedFile + " must not exist — "
                + "if this fails, either the sanitiser regressed or a previous "
                + "test run leaked an artifact (delete " + escapedFile + ").");
    }

    @Test
    void should_rejectKeyTraversal_when_keyContainsDotDot() throws IOException {
        backend.save("alice", "../../../etc/passwd", "pwned");

        Path safeFile = baseDir.resolve("alice").resolve(".._.._.._etc_passwd.json");
        Path escapedFile = baseDir.resolve("alice").resolve("../../../etc/passwd.json").normalize();

        assertTrue(Files.exists(safeFile));
        assertEquals("pwned", Files.readString(safeFile));
        assertFalse(Files.exists(escapedFile));
    }

    @Test
    void should_sanitizeUnsafeChars_when_tenantHasSlashes() throws IOException {
        backend.save("a/b", "k", "v");

        Path safeFile = baseDir.resolve("a_b").resolve("k.json");

        assertTrue(Files.exists(safeFile));
        assertEquals("v", Files.readString(safeFile));
    }

    @Test
    void should_writeAtomically_when_concurrent_savesOnSameKey() {
        List<String> values = IntStream.range(0, 20)
                .mapToObj(i -> "{\"value\":\"v" + i + "\",\"payload\":\"" + "x".repeat(256) + "\"}")
                .toList();

        CompletableFuture<?>[] futures = values.stream()
                .map(value -> CompletableFuture.runAsync(() -> backend.save("alice", "k", value)))
                .toArray(CompletableFuture[]::new);

        CompletableFuture.allOf(futures).join();

        String stored = backend.load("alice", "k");

        assertNotNull(stored);
        assertFalse(stored.isEmpty());
        assertTrue(values.contains(stored));
    }

    @Test
    void should_leaveOldValue_when_saveFailsMidWrite() throws IOException {
        backend.save("alice", "k", "v0");
        assertEquals("v0", backend.load("alice", "k"));

        Path tenantDir = baseDir.resolve("alice");
        PermissionReset permissionReset = makeDirectoryReadOnly(tenantDir);
        try {
            assertThrows(RuntimeException.class, () -> backend.save("alice", "k", "corrupted"));
        } finally {
            permissionReset.restore();
        }

        assertEquals("v0", backend.load("alice", "k"));
    }

    @Test
    void should_listOnlyTheSpecifiedTenant_when_listingKeys() {
        backend.save("alice", "a", "1");
        backend.save("alice", "b", "2");
        backend.save("bob", "c", "3");

        assertEquals(Set.of("a", "b"), Set.copyOf(backend.list("alice")));
    }

    @Test
    void should_returnEmptyList_when_tenantHasTraversal() {
        assertTrue(backend.list("../other").isEmpty());
    }

    private PermissionReset makeDirectoryReadOnly(Path directory) throws IOException {
        if (Files.getFileStore(directory).supportsFileAttributeView("posix")) {
            Set<PosixFilePermission> originalPermissions = Files.getPosixFilePermissions(directory);
            Set<PosixFilePermission> readOnlyPermissions = EnumSet.copyOf(originalPermissions);
            readOnlyPermissions.remove(PosixFilePermission.OWNER_WRITE);
            readOnlyPermissions.remove(PosixFilePermission.GROUP_WRITE);
            readOnlyPermissions.remove(PosixFilePermission.OTHERS_WRITE);
            Files.setPosixFilePermissions(directory, readOnlyPermissions);
            return () -> Files.setPosixFilePermissions(directory, originalPermissions);
        }

        boolean wasWritable = directory.toFile().canWrite();
        if (!directory.toFile().setWritable(false, false)) {
            throw new IOException("Failed to make directory read-only");
        }
        return () -> {
            if (!directory.toFile().setWritable(wasWritable, false)) {
                throw new IOException("Failed to restore directory permissions");
            }
        };
    }

    @FunctionalInterface
    private interface PermissionReset {
        void restore() throws IOException;
    }
}
