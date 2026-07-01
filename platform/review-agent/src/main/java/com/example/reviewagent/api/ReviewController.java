package com.example.reviewagent.api;

import com.example.reviewagent.agent.ReviewAgent;
import com.example.reviewagent.tool.FileTools;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;

import java.util.Map;
import java.util.UUID;

/**
 * REST API for the code review agent.
 *
 * POST /api/review        — synchronous review (waits for full response)
 * POST /api/review/stream  — SSE streaming review (token by token)
 * POST /api/workspace      — set the workspace root path
 * GET  /health             — health check
 */
@RestController
public class ReviewController {

    private final ReviewAgent agent;
    private final FileTools fileTools;

    public ReviewController(ReviewAgent agent, FileTools fileTools) {
        this.agent = agent;
        this.fileTools = fileTools;
    }

    public record ReviewRequest(String message, String sessionId) {}
    public record WorkspaceRequest(String path) {}

    @PostMapping("/api/review")
    public Map<String, String> review(@RequestBody ReviewRequest request) {
        String sessionId = request.sessionId() != null ? request.sessionId() : UUID.randomUUID().toString();
        String response = agent.review(request.message(), sessionId);
        return Map.of("response", response, "sessionId", sessionId);
    }

    @PostMapping(value = "/api/review/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<String> reviewStream(@RequestBody ReviewRequest request) {
        String sessionId = request.sessionId() != null ? request.sessionId() : UUID.randomUUID().toString();
        return agent.reviewStream(request.message(), sessionId);
    }

    @PostMapping("/api/workspace")
    public Map<String, String> setWorkspace(@RequestBody WorkspaceRequest request) {
        fileTools.setWorkspaceRoot(request.path());
        return Map.of("workspace", request.path(), "status", "ok");
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of(
                "status", "ok",
                "service", "review-agent",
                "workspace", fileTools.getWorkspaceRoot()
        );
    }
}
