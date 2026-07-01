package com.example.reviewagent.tool;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Tests for FileTools — validates read-only file operations and security.
 */
class FileToolsTest {

    private FileTools fileTools;

    @TempDir
    Path tempDir;

    @BeforeEach
    void setUp() throws IOException {
        fileTools = new FileTools();
        fileTools.setWorkspaceRoot(tempDir.toString());

        // Create test files
        Files.writeString(tempDir.resolve("README.md"), "# Test Project\nThis is a test.");
        Files.createDirectories(tempDir.resolve("src/main"));
        Files.writeString(tempDir.resolve("src/main/App.java"),
                "package com.example;\n\npublic class App {\n    public static void main(String[] args) {\n        System.out.println(\"Hello\");\n    }\n}\n");
        Files.writeString(tempDir.resolve("src/main/Service.java"),
                "package com.example;\n\npublic class Service {\n    // TODO: implement\n}\n");
    }

    @Test
    void fileList_returnsTree() {
        String result = fileTools.file_list(null, 3);
        assertTrue(result.contains("README.md"));
        assertTrue(result.contains("src"));
        assertTrue(result.contains("App.java"));
    }

    @Test
    void fileRead_withLineNumbers() {
        String result = fileTools.file_read("src/main/App.java", null, null);
        assertTrue(result.contains("1 | package com.example;"));
        assertTrue(result.contains("4 |     public static void main"));
        assertTrue(result.contains("7 lines total"));
    }

    @Test
    void fileRead_withRange() {
        String result = fileTools.file_read("src/main/App.java", 3, 5);
        assertTrue(result.contains("3 | public class App"));
        assertFalse(result.contains("1 | package"));
    }

    @Test
    void fileRead_notFound() {
        String result = fileTools.file_read("nonexistent.txt", null, null);
        assertTrue(result.contains("❌"));
    }

    @Test
    void fileSearch_findsMatches() {
        String result = fileTools.file_search("System.out", null);
        assertTrue(result.contains("App.java"));
        assertTrue(result.contains("println"));
    }

    @Test
    void fileSearch_noMatches() {
        String result = fileTools.file_search("xyzzy_nonexistent_string", null);
        assertTrue(result.contains("No matches"));
    }

    @Test
    void fileSearch_withExtensionFilter() {
        String result = fileTools.file_search("package", "java");
        assertTrue(result.contains("App.java"));
        assertFalse(result.contains("README.md"));
    }

    @Test
    void pathTraversal_blocked() {
        assertThrows(SecurityException.class, () -> fileTools.file_read("../../etc/passwd", null, null));
    }

    @Test
    void fileList_emptyDir() throws IOException {
        Path emptyDir = tempDir.resolve("empty");
        Files.createDirectory(emptyDir);
        String result = fileTools.file_list("empty", 1);
        // Empty dir should return empty or just the dir itself
        assertNotNull(result);
    }
}
