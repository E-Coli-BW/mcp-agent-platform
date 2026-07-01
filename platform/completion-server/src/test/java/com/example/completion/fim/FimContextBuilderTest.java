package com.example.completion.fim;

import com.example.completion.config.CompletionProperties;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class FimContextBuilderTest {

    private FimContextBuilder builder;

    @BeforeEach
    void setUp() {
        CompletionProperties props = new CompletionProperties();
        props.getFim().setMaxPrefixLines(5);
        props.getFim().setMaxSuffixLines(3);
        builder = new FimContextBuilder(props);
    }

    @Test
    void basicCursorPosition() {
        String file = "line0\nline1\nline2\nline3\nline4";
        var ctx = builder.build(file, 2, 3); // cursor at line 2, col 3
        assertEquals("line0\nline1\nlin", ctx.prefix());
        assertTrue(ctx.suffix().startsWith("e2"));
    }

    @Test
    void cursorAtStartOfLine() {
        String file = "aaa\nbbb\nccc";
        var ctx = builder.build(file, 1, 0);
        assertEquals("aaa\n", ctx.prefix());
        assertTrue(ctx.suffix().startsWith("bbb"));
    }

    @Test
    void cursorAtEndOfFile() {
        String file = "hello\nworld";
        var ctx = builder.build(file, 1, 5);
        assertEquals("hello\nworld", ctx.prefix());
        assertEquals("", ctx.suffix());
    }

    @Test
    void prefixTruncatedToMaxLines() {
        // 10 lines, but max prefix is 5
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 10; i++) sb.append("line").append(i).append('\n');
        String file = sb.toString().trim();
        var ctx = builder.build(file, 9, 0); // cursor at last line
        // Prefix should only have last 5 lines before cursor line
        String[] prefixLines = ctx.prefix().split("\n", -1);
        assertTrue(prefixLines.length <= 6, "prefix should be truncated to ~5 lines");
    }

    @Test
    void suffixTruncatedToMaxLines() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 10; i++) sb.append("line").append(i).append('\n');
        String file = sb.toString().trim();
        var ctx = builder.build(file, 0, 5); // cursor in first line
        String[] suffixLines = ctx.suffix().split("\n", -1);
        assertTrue(suffixLines.length <= 4, "suffix should be truncated to ~3 lines");
    }
}
