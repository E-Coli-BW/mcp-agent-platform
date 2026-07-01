package com.example.agent.rag;

import org.springframework.stereotype.Component;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * AST-aware context compression with regex fallbacks.
 */
@Component
public class AstCompressor {

    private static final Pattern PYTHON_DEF = Pattern.compile("^(\\s*def\\s+\\w+\\s*\\([^)]*\\)[^:]*:)", Pattern.MULTILINE);
    private static final Pattern PYTHON_CLASS = Pattern.compile("^(\\s*class\\s+\\w+[^:]*:)", Pattern.MULTILINE);
    private static final Pattern JAVA_METHOD = Pattern.compile("^(\\s*(?:public|private|protected|static|final|abstract|synchronized|@\\w+\\s+)*[\\w<>\\[\\]]+\\s+\\w+\\s*\\([^)]*\\))", Pattern.MULTILINE);
    private static final Pattern RETURN_STMT = Pattern.compile("^\\s*return\\s+.*$", Pattern.MULTILINE);
    private static final Pattern DOCSTRING = Pattern.compile("\"\"\"(.*?)\"\"\"", Pattern.DOTALL);

    /**
     * Compresses code using structural extraction when possible.
     */
    public String compress(String code, int maxChars, String toolName) {
        if (code == null || code.length() <= maxChars) {
            return code;
        }
        if (maxChars <= 0) {
            return "";
        }
        if ("file_read".equals(toolName) || "rag_search".equals(toolName)) {
            return astCompress(code, maxChars);
        }
        return headTail(code, maxChars);
    }

    private String astCompress(String code, int maxChars) {
        StringBuilder skeleton = new StringBuilder();
        extractMatches(PYTHON_DEF, code, skeleton);
        extractMatches(PYTHON_CLASS, code, skeleton);
        extractMatches(JAVA_METHOD, code, skeleton);
        Matcher returnMatcher = RETURN_STMT.matcher(code);
        while (returnMatcher.find() && skeleton.length() < maxChars) {
            skeleton.append(returnMatcher.group().trim()).append("\n");
        }
        Matcher docMatcher = DOCSTRING.matcher(code);
        while (docMatcher.find() && skeleton.length() < maxChars) {
            String doc = docMatcher.group(1).trim();
            skeleton.append("# ").append(doc, 0, Math.min(100, doc.length())).append("\n");
        }
        if (skeleton.length() > maxChars) {
            return skeleton.substring(0, maxChars);
        }
        if (skeleton.isEmpty()) {
            return headTail(code, maxChars);
        }
        return skeleton.toString();
    }

    private void extractMatches(Pattern pattern, String code, StringBuilder out) {
        Matcher matcher = pattern.matcher(code);
        while (matcher.find()) {
            out.append(matcher.group().trim()).append("\n");
        }
    }

    private String headTail(String code, int maxChars) {
        int half = maxChars / 2;
        int omitted = code.length() - maxChars;
        return code.substring(0, half) + "\n...(truncated " + omitted + " chars)...\n" + code.substring(code.length() - half);
    }
}
