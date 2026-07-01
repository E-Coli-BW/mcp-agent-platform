package com.example.agent;

import com.example.agent.agent.ContextCompressor;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class ContextCompressorTest {

    private final ContextCompressor compressor = new ContextCompressor();

    @Test
    void should_truncateWithHeadTail_when_longContent() {
        String content = "A".repeat(5000);
        String result = compressor.smartCompress(content, 1000, "code_run");
        assertTrue(result.length() <= 1100);
        assertTrue(result.contains("truncated"));
    }

    @Test
    void should_keepFirstAndLastLines_when_listing() {
        StringBuilder listing = new StringBuilder();
        for (int i = 1; i <= 50; i++) {
            listing.append("file").append(i).append(".py\n");
        }
        String result = compressor.smartCompress(listing.toString(), 5000, "file_list");
        assertTrue(result.contains("file1.py"));
        assertTrue(result.contains("file50.py"));
        assertTrue(result.contains("lines omitted"));
    }

    @Test
    void should_returnOriginal_when_shortContent() {
        String content = "short text";
        assertEquals(content, compressor.smartCompress(content, 1000, "file_read"));
    }

    @Test
    void should_returnEmpty_when_maxCharsZero() {
        assertEquals("", compressor.smartCompress("anything", 0, "file_read"));
    }
}
