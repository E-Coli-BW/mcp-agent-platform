package com.example.completion.api;

import com.example.completion.cache.CompletionCache;
import com.example.completion.config.CompletionProperties;
import com.example.completion.fim.FimContextBuilder;
import com.example.completion.fim.FimContextBuilder.FimContext;
import com.example.completion.fim.FimFormatter;
import com.example.completion.fim.FimFormatterRegistry;
import com.example.completion.metrics.CompletionMetrics;
import com.example.completion.proxy.OllamaStreamingProxy;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * POST /v1/completions — OpenAI-compatible FIM code completion.
 *
 * Flow:
 * 1. Parse request (raw FIM prompt or file+cursor)
 * 2. Check prefix cache → return immediately if hit
 * 3. Build FIM prompt using the right formatter
 * 4. Stream tokens from Ollama via WebClient
 * 5. Record TTFT and token metrics
 * 6. Cache the result for future hits
 */
@RestController
public class CompletionController {

    private static final Logger log = LoggerFactory.getLogger(CompletionController.class);

    private final FimFormatterRegistry formatterRegistry;
    private final FimContextBuilder contextBuilder;
    private final OllamaStreamingProxy proxy;
    private final CompletionCache cache;
    private final CompletionMetrics metrics;
    private final CompletionProperties props;
    private final ObjectMapper mapper = new ObjectMapper();

    public CompletionController(
            FimFormatterRegistry formatterRegistry,
            FimContextBuilder contextBuilder,
            OllamaStreamingProxy proxy,
            CompletionCache cache,
            CompletionMetrics metrics,
            CompletionProperties props) {
        this.formatterRegistry = formatterRegistry;
        this.contextBuilder = contextBuilder;
        this.proxy = proxy;
        this.cache = cache;
        this.metrics = metrics;
        this.props = props;
    }

    /**
     * Streaming completion endpoint.
     * Returns SSE stream of OpenAI-compatible completion chunks.
     */
    @PostMapping(value = "/v1/completions", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<String> completions(@RequestBody CompletionRequest request) {
        String model = request.model() != null ? request.model() : props.getOllama().getModel();
        List<String> stopTokens = request.stop() != null ? request.stop() : props.getFim().getStopTokens();

        // Step 1: Build FIM context
        FimContext ctx;
        if (request.isRawPrompt()) {
            // Raw prompt mode — prompt already contains FIM tokens
            return streamFromOllama(request.prompt(), model, stopTokens, request.prompt());
        } else if (request.fileContent() != null && request.cursorLine() != null) {
            ctx = contextBuilder.build(
                    request.fileContent(),
                    request.cursorLine(),
                    request.cursorColumn() != null ? request.cursorColumn() : 0
            );
        } else {
            return Flux.just(formatSSE("Error: provide 'prompt' or 'file_content' + 'cursor_line'", null, "error"));
        }

        // Step 2: Check cache
        var cached = cache.get(ctx.prefix(), model);
        if (cached.isPresent()) {
            metrics.recordCacheHit();
            log.debug("Cache hit for model={}", model);
            return Flux.just(
                    formatSSE(cached.get(), null, null),
                    formatSSE("", null, "stop"),
                    "[DONE]"
            );
        }
        metrics.recordCacheMiss();

        // Step 3: Build FIM prompt
        FimFormatter formatter = formatterRegistry.getFormatter(model);
        String fimPrompt = formatter.format(ctx.prefix(), ctx.suffix());

        return streamFromOllama(fimPrompt, model, stopTokens, ctx.prefix());
    }

    /**
     * Non-streaming version for simple clients.
     */
    @PostMapping(value = "/v1/completions", produces = MediaType.APPLICATION_JSON_VALUE)
    public Mono<Map<String, Object>> completionsSync(@RequestBody CompletionRequest request) {
        String model = request.model() != null ? request.model() : props.getOllama().getModel();
        List<String> stopTokens = request.stop() != null ? request.stop() : props.getFim().getStopTokens();

        FimContext ctx;
        if (request.fileContent() != null && request.cursorLine() != null) {
            ctx = contextBuilder.build(request.fileContent(), request.cursorLine(),
                    request.cursorColumn() != null ? request.cursorColumn() : 0);
        } else {
            return Mono.just(Map.of("error", "provide file_content + cursor_line"));
        }

        var cached = cache.get(ctx.prefix(), model);
        if (cached.isPresent()) {
            metrics.recordCacheHit();
            return Mono.just(Map.of(
                    "id", "cmpl-" + UUID.randomUUID().toString().substring(0, 8),
                    "choices", List.of(Map.of("text", cached.get(), "finish_reason", "stop"))
            ));
        }

        FimFormatter formatter = formatterRegistry.getFormatter(model);
        String fimPrompt = formatter.format(ctx.prefix(), ctx.suffix());

        return proxy.generate(fimPrompt, model, stopTokens)
                .reduce(new StringBuilder(), StringBuilder::append)
                .map(sb -> {
                    String text = sb.toString();
                    cache.put(ctx.prefix(), model, text);
                    metrics.recordCompleted();
                    return Map.<String, Object>of(
                            "id", "cmpl-" + UUID.randomUUID().toString().substring(0, 8),
                            "choices", List.of(Map.of("text", text, "finish_reason", "stop"))
                    );
                });
    }

    // ── Private helpers ──────────────────────────────────────────

    private Flux<String> streamFromOllama(String fimPrompt, String model, List<String> stopTokens, String prefixForCache) {
        Instant start = Instant.now();
        AtomicBoolean firstToken = new AtomicBoolean(true);
        AtomicInteger tokenCount = new AtomicInteger(0);
        StringBuilder fullCompletion = new StringBuilder();

        return proxy.generate(fimPrompt, model, stopTokens)
                .map(token -> {
                    // Record TTFT on first token
                    if (firstToken.compareAndSet(true, false)) {
                        Duration ttft = Duration.between(start, Instant.now());
                        metrics.recordTtft(ttft);
                        log.info("TTFT: {}ms for model={}", ttft.toMillis(), model);
                    }
                    tokenCount.incrementAndGet();
                    fullCompletion.append(token);
                    return formatSSE(token, null, null);
                })
                .concatWith(Flux.defer(() -> {
                    // Completion done — cache result, record metrics
                    metrics.recordTokens(tokenCount.get());
                    metrics.recordCompleted();
                    if (prefixForCache != null && !fullCompletion.isEmpty()) {
                        cache.put(prefixForCache, model, fullCompletion.toString());
                    }
                    return Flux.just(
                            formatSSE("", null, "stop"),
                            "[DONE]"
                    );
                }))
                .doOnCancel(() -> metrics.recordCancelled());
    }

    private String formatSSE(String text, String id, String finishReason) {
        try {
            ObjectNode chunk = mapper.createObjectNode();
            chunk.put("id", id != null ? id : "cmpl-" + UUID.randomUUID().toString().substring(0, 8));
            chunk.put("object", "text_completion");
            var choices = chunk.putArray("choices");
            var choice = choices.addObject();
            choice.put("text", text);
            choice.put("index", 0);
            if (finishReason != null) choice.put("finish_reason", finishReason);
            return mapper.writeValueAsString(chunk);
        } catch (Exception e) {
            return "{\"error\":\"" + e.getMessage() + "\"}";
        }
    }

    /**
     * Health endpoint.
     */
    @GetMapping("/health")
    public Mono<Map<String, String>> health() {
        return Mono.just(Map.of(
                "status", "ok",
                "service", "completion-server",
                "model", props.getOllama().getModel()
        ));
    }
}
