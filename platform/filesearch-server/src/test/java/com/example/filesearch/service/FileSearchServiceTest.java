package com.example.filesearch.service;

import com.example.filesearch.sandbox.PathSandbox;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for FileSearchService — uses real temp directory, no mocks.
 */
class FileSearchServiceTest {

    @TempDir
    Path tempDir;

    private FileSearchService service;

    @BeforeEach
    void setUp() throws IOException {
        var sandbox = new PathSandbox(tempDir.toRealPath().toString());
        service = new FileSearchService(sandbox, 100, 200, 10 * 1024 * 1024, 5);

        // Create test files
        Files.writeString(tempDir.resolve("hello.txt"), "Hello World\nLine 2\nLine 3\n");
        Files.writeString(tempDir.resolve("code.java"), "public class Main {\n  public static void main(String[] args) {\n    System.out.println(\"hello\");\n  }\n}\n");

        Path subDir = tempDir.resolve("src");
        Files.createDirectories(subDir);
        Files.writeString(subDir.resolve("app.py"), "print('hello from python')\nimport os\n");
        Files.writeString(subDir.resolve("README.md"), "# My Project\nThis is a test.\n");
    }

    private String p(String name) throws IOException {
        return tempDir.toRealPath().resolve(name).toString();
    }

    // ── file_read ────────────────────────────────────────────────

    @Test
    void readFile_fullContent() throws IOException {
        String result = service.readFile("t1", p("hello.txt"), null, null);
        assertTrue(result.contains("Hello World"));
    }

    @Test
    void readFile_lineRange() throws IOException {
        String result = service.readFile("t1", p("hello.txt"), 2, 3);
        assertFalse(result.contains("Hello World"));
        assertTrue(result.contains("Line 2"));
    }

    @Test
    void readFile_notFound() throws IOException {
        String result = service.readFile("t1", p("nope.txt"), null, null);
        assertTrue(result.contains("not found"));
    }

    @Test
    void readFile_outsideSandbox_blocked() {
        assertThrows(SecurityException.class,
                () -> service.readFile("t1", "/etc/passwd", null, null));
    }

    @Test
    void search_findsMatches() throws IOException {
        String result = service.search("t1", "hello", tempDir.toRealPath().toString(), null, false, null);
        assertTrue(result.contains("match"));
    }

    @Test
    void search_noMatches() throws IOException {
        String result = service.search("t1", "xyznonexistent123", tempDir.toRealPath().toString(), null, false, null);
        assertTrue(result.contains("No matches"));
    }

    @Test
    void search_caseInsensitive() throws IOException {
        String result = service.search("t1", "HELLO", tempDir.toRealPath().toString(), null, true, null);
        assertTrue(result.contains("match"));
    }

    @Test
    void listDirectory_showsEntries() throws IOException {
        String result = service.listDirectory("t1", tempDir.toRealPath().toString());
        assertTrue(result.contains("hello.txt"));
        assertTrue(result.contains("src/"));
    }

    @Test
    void tree_showsStructure() throws IOException {
        String result = service.tree("t1", tempDir.toRealPath().toString(), 3);
        assertTrue(result.contains("hello.txt"));
        assertTrue(result.contains("src/"));
    }

    @Test
    void stat_showsMetadata() throws IOException {
        String result = service.stat("t1", p("hello.txt"));
        assertTrue(result.contains("file"));
    }

    @Test
    void glob_findsMatchingFiles() throws IOException {
        String result = service.glob("t1", "*.java", tempDir.toRealPath().toString(), null);
        assertTrue(result.contains("code.java"));
    }

    @Test
    void glob_noMatches() throws IOException {
        String result = service.glob("t1", "*.xyz", tempDir.toRealPath().toString(), null);
        assertTrue(result.contains("No files"));
    }
}
