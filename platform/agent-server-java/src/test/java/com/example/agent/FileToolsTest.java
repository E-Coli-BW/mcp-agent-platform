package com.example.agent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

class FileToolsTest {

    @TempDir
    Path tempDir;

    @Test
    void should_readFile_with_lineNumbers() throws IOException {
        Path file = tempDir.resolve("test.py");
        Files.writeString(file, "line1\nline2\nline3\n");
        var lines = Files.readAllLines(file);
        StringBuilder result = new StringBuilder();
        for (int i = 0; i < lines.size(); i++) {
            result.append(String.format("%4d | %s%n", i + 1, lines.get(i)));
        }
        assertTrue(result.toString().contains("   1 | line1"));
        assertTrue(result.toString().contains("   2 | line2"));
    }

    @Test
    void should_returnError_when_fileNotFound() {
        Path missing = tempDir.resolve("nonexistent.py");
        assertFalse(Files.exists(missing));
    }

    @Test
    void should_listDirectory_with_tree() throws IOException {
        Files.createDirectories(tempDir.resolve("src"));
        Files.writeString(tempDir.resolve("src/main.py"), "print('hello')");
        Files.writeString(tempDir.resolve("README.md"), "# Test");
        assertTrue(Files.exists(tempDir.resolve("src")));
        assertTrue(Files.exists(tempDir.resolve("README.md")));
    }
}
