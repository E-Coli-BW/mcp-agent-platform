package com.example.agent.workspace;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class WorkspaceResolverTest {

    @TempDir
    Path tempDir;

    @Test
    void should_giveEachTenantDistinctDirectory_when_multiTenant() throws IOException {
        Path basePath = tempDir.toRealPath();
        WorkspaceResolver resolver = new WorkspaceResolver(basePath.toString(), true);

        Path tenantOne = resolver.forTenant("tenant-one");
        Path tenantTwo = resolver.forTenant("tenant-two");

        assertNotEquals(tenantOne, tenantTwo);
        assertEquals(basePath.resolve("tenant-one"), tenantOne);
        assertEquals(basePath.resolve("tenant-two"), tenantTwo);
        assertTrue(Files.isDirectory(tenantOne));
        assertTrue(Files.isDirectory(tenantTwo));
    }

    @Test
    void should_shareBase_when_singleTenant() throws IOException {
        Path basePath = tempDir.toRealPath();
        WorkspaceResolver resolver = new WorkspaceResolver(basePath.toString(), false);

        assertEquals(basePath, resolver.forTenant("tenant-one"));
        assertEquals(basePath, resolver.forTenant("tenant-two"));
    }

    @Test
    void should_sanitizeTenantId_inDirectoryName() throws IOException {
        Path basePath = tempDir.toRealPath();
        WorkspaceResolver resolver = new WorkspaceResolver(basePath.toString(), true);

        Path tenantPath = resolver.forTenant("../../etc");

        assertEquals(basePath.resolve(".._.._etc"), tenantPath);
    }

    @Test
    void should_rejectAbsolutePath() throws IOException {
        Path workspaceRoot = Files.createDirectories(tempDir.toRealPath().resolve("workspace"));

        assertThrows(SecurityException.class, () -> WorkspaceResolver.validate(workspaceRoot, "/etc/passwd"));
    }

    @Test
    void should_rejectDotDotEscape() throws IOException {
        Path workspaceRoot = Files.createDirectories(tempDir.toRealPath().resolve("workspace"));

        assertThrows(SecurityException.class,
                () -> WorkspaceResolver.validate(workspaceRoot, "../../etc/passwd"));
    }

    @Test
    void should_rejectSymlinkOutsideWorkspace() throws IOException {
        Path workspaceRoot = Files.createDirectories(tempDir.toRealPath().resolve("workspace"));
        Path outsideRoot = Files.createDirectories(tempDir.resolve("outside"));
        Path outsideFile = Files.writeString(outsideRoot.resolve("secret.txt"), "secret");
        Files.createSymbolicLink(workspaceRoot.resolve("escape.txt"), outsideFile);

        assertThrows(SecurityException.class,
                () -> WorkspaceResolver.validate(workspaceRoot, "escape.txt"));
    }

    @Test
    void should_acceptSymlinkInsideWorkspace() throws IOException {
        Path workspaceRoot = Files.createDirectories(tempDir.toRealPath().resolve("workspace"));
        Path targetFile = Files.writeString(workspaceRoot.resolve("safe.txt"), "safe");
        Path symlink = workspaceRoot.resolve("inside-link.txt");
        Files.createSymbolicLink(symlink, targetFile);

        Path validated = WorkspaceResolver.validate(workspaceRoot, "inside-link.txt");

        assertEquals(symlink.normalize(), validated);
    }
}
