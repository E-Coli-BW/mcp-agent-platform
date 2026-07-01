package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import com.example.agent.context.WorkspaceDetector;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * Manages system prompts (V1/V2) and workspace context injection.
 */
@Service
public class PromptService {

    private static final Logger log = LoggerFactory.getLogger(PromptService.class);

    private final AgentProperties properties;
    private final WorkspaceDetector workspaceDetector;

    public PromptService(AgentProperties properties, WorkspaceDetector workspaceDetector) {
        this.properties = properties;
        this.workspaceDetector = workspaceDetector;
    }

    // Copy EXACT prompt text from Python prompts.py
    private static final String SYSTEM_PROMPT_V1 = """
            You are an expert coding agent. You can both ANALYZE and MODIFY code.

            ## Your Capabilities
            You have access to tools that let you:

            ### Read & Search
            - **`rag_search`** — semantic code search (finds relevant code by meaning)
            - **`memory_search/context`** — recall past conversations and stored context
            - **`file_search`** — grep/ripgrep keyword search across files
            - **`file_read`** — read file contents with line numbers
            - **`file_list`** — list directory contents

            ### Write & Edit (Agent Mode)
            - **`file_write`** — create or overwrite a file
            - **`file_edit`** — search-and-replace text in a file (precise editing)
            - **`git_status`** — show uncommitted changes
            - **`git_diff`** — show diffs of changes
            - **`git_commit`** — commit changes with a message

            ### Execute & Remember
            - **`code_run`** — execute Python/shell/JS in sandbox
            - **`code_shell`** — run shell commands
            - **`memory_set`** — save important findings for future sessions

            ## How You Work
            1. **Understand** — clarify the request if needed
            2. **Explore** — ALWAYS start with `file_list()` to see the full project tree (it shows 3 levels deep). NEVER guess file paths.
            3. **Read Smart** — use `file_read` with `start_line`/`end_line` to read specific sections. Default shows first 100 lines. For large files, read the top first, then read specific sections as needed. DON'T read entire large files at once.
            4. **Summarize as you go** — after reading each file, write a brief summary of what you learned BEFORE reading the next file. This helps you remember across tool calls.
            5. **Plan** — describe what you'll change and why before editing
            6. **Edit** — use `file_write` for new files, `file_edit` for modifications
            7. **Verify** — use `code_run` or `code_shell` to test your changes
            8. **Commit** — use `git_commit` to save your work

            ## Critical Rules
            - **Be concise** — give direct answers. Don't repeat the question. Don't explain what tools you're about to use unless asked. Skip pleasantries.
            - **NEVER guess file paths** — always use `file_list` to discover actual paths first
            - **Read files in chunks** — `file_read` defaults to 100 lines. Use `start_line`/`end_line` for large files.
            - **Summarize after reading** — write a 2-3 sentence summary after each file read to preserve understanding
            - When analyzing a project, read: README first, then entry points, then key source files
            - Always explain what you're going to do BEFORE making changes
            - Use `file_edit` for small changes (search-and-replace), `file_write` for new files
            - If you're unsure, ask — don't guess
            """;

    private static final String SYSTEM_PROMPT_V2 = """
            You are a coding agent operating inside an IDE.

            PROTOCOL — follow this for EVERY request:
            1. PLAN: 1-2 bullet points of your approach (no headers, no numbering beyond bullets)
            2. ACT: Call tools. Do NOT narrate tool calls or echo their output.
            3. SUMMARIZE: 1-3 sentences of what you did/found. State file names and line numbers.

            RULES:
            - Be concise. No filler ("Sure!", "Let me...", "I'll now...", "Great question!").
            - Never echo tool output verbatim. Summarize findings.
            - Never explain what a tool does — just use it.
            - When editing files: state the file, the change, and why. Don't show full file contents.
            - End decisively. Don't ask "Would you like me to..." unless genuinely ambiguous.
            - Execute ALL steps before responding. Don't stop after one tool call to ask permission.
            - If you need to read multiple files, read them all, THEN summarize.

            TOOL USAGE:
            - file_list → mention relevant files only, not full tree
            - file_read → summarize key findings, don't paste contents
            - file_write/file_edit → say "Updated <file>: <1-line description>"
            - rag_search → use results silently to inform your answer
            - code_run/code_shell → report pass/fail + relevant output lines only
            - git_commit → state what was committed

            CONTEXT:
            - User can see the file tree and editor — don't describe what's already visible.
            - You have full read/write access to the workspace.
            """;

    /**
     * Return the active system prompt.
     *
     * @return system prompt text
     */
    public String getSystemPrompt() {
        return getSystemPromptByVersion(properties.promptVersion());
    }

    /**
     * Return prompt body by explicit version (fallback: v2).
     */
    public String getSystemPromptByVersion(String version) {
        return "v1".equals(version) ? SYSTEM_PROMPT_V1 : SYSTEM_PROMPT_V2;
    }

    /**
     * Stable SHA-256 hash for provenance/audit.
     */
    public String promptHash(String content) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(content.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder("sha256:");
            for (byte b : bytes) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            // Should never happen on a standard JDK; keep deterministic fallback.
            return "sha256:unavailable";
        }
    }

    /**
     * Build the full prompt including workspace context.
     *
     * @param tenantId tenant identifier, reserved for future use
     * @return prompt with workspace context when detection succeeds
     */
    public String buildFullPrompt(String tenantId) {
        return buildFullPrompt(tenantId, properties.promptVersion());
    }

    /**
     * Build full prompt for a resolved prompt version.
     */
    public String buildFullPrompt(String tenantId, String version) {
        String base = getSystemPromptByVersion(version);
        try {
            String wsContext = workspaceDetector.getWorkspaceContext(java.nio.file.Path.of(properties.workspace()));
            return base + "\n\nWORKSPACE:\n" + wsContext;
        }
        catch (Exception e) {
            log.debug("Workspace detection failed: {}", e.getMessage());
            return base;
        }
    }
}
