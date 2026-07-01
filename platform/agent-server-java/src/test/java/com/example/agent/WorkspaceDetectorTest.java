package com.example.agent;

import com.example.agent.context.WorkspaceDetector;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

class WorkspaceDetectorTest {

    @TempDir
    Path tempDir;

    private final WorkspaceDetector detector = new WorkspaceDetector();

    @Test
    void should_detectJavaMaven_when_pomExists() throws IOException {
        Files.writeString(tempDir.resolve("pom.xml"), "<project/>");
        String type = detector.detectProjectType(tempDir);
        assertEquals("Java/Maven", type);
    }

    @Test
    void should_detectPython_when_pyprojectExists() throws IOException {
        Files.writeString(tempDir.resolve("pyproject.toml"), "[project]");
        String type = detector.detectProjectType(tempDir);
        assertEquals("Python", type);
    }

    @Test
    void should_returnNull_when_noMarkers() {
        assertNull(detector.detectProjectType(tempDir));
    }

    @Test
    void should_detectModules_when_subdirsHaveMarkers() throws IOException {
        Files.createDirectories(tempDir.resolve("service-a"));
        Files.writeString(tempDir.resolve("service-a/pom.xml"), "<project/>");
        Files.createDirectories(tempDir.resolve("frontend"));
        Files.writeString(tempDir.resolve("frontend/package.json"), "{}");
        var modules = detector.detectModules(tempDir);
        assertTrue(modules.size() >= 2);
    }

    @Test
    void should_readSummary_when_readmeExists() throws IOException {
        Files.writeString(tempDir.resolve("README.md"), "# My Project\nThis is a test project.");
        String summary = detector.readSummary(tempDir);
        assertNotNull(summary);
        assertTrue(summary.contains("My Project"));
    }
}
