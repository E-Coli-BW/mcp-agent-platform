package com.example.completion.proxy;

import com.example.completion.config.CompletionProperties;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;

import java.time.Duration;
import java.util.List;

/**
 * Streaming proxy to Ollama /api/generate endpoint.
 *
 * Uses WebClient (non-blocking, Netty) to stream SSE responses from Ollama.
 * The Flux<String> returned by generate() emits each token as it arrives.
 * 
 * Key Reactor features used:
 * - Flux.create() for SSE parsing
 * - timeout() for TTFT enforcement
 * - takeUntil() for stop token detection
 * - doOnCancel() for request cancellation propagation
 */
@Component
public class OllamaStreamingProxy {

    private static final Logger log = LoggerFactory.getLogger(OllamaStreamingProxy.class);

    private final WebClient webClient;
    private final CompletionProperties props;
    private final ObjectMapper mapper;

    public OllamaStreamingProxy(CompletionProperties props) {
        this.props = props;
        this.mapper = new ObjectMapper();
        this.webClient = WebClient.builder()
                .baseUrl(props.getOllama().getBaseUrl())
                .build();
    }

    /**
     * Stream FIM completion from Ollama.
     *
     * @param fimPrompt formatted FIM prompt (with special tokens)
     * @param model Ollama model name
     * @param stopTokens list of strings that should stop generation
     * @return Flux of completion text tokens
     */
    public Flux<String> generate(String fimPrompt, String model, List<String> stopTokens) {
        ObjectNode requestBody = mapper.createObjectNode();
        requestBody.put("model", model != null ? model : props.getOllama().getModel());
        requestBody.put("prompt", fimPrompt);
        requestBody.put("stream", true);

        // Options
        ObjectNode options = mapper.createObjectNode();
        options.put("temperature", props.getFim().getTemperature());
        options.put("num_predict", props.getFim().getMaxTokens());
        if (stopTokens != null && !stopTokens.isEmpty()) {
            var stopArray = options.putArray("stop");
            stopTokens.forEach(stopArray::add);
        }
        requestBody.set("options", options);

        requestBody.put("raw", true); // Send prompt as-is (FIM tokens already included)

        log.debug("Ollama generate: model={}, prompt_length={}", model, fimPrompt.length());

        return webClient.post()
                .uri("/api/generate")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(requestBody.toString())
                .retrieve()
                .bodyToFlux(String.class)
                .timeout(Duration.ofMillis(props.getOllama().getTimeoutMs()))
                .mapNotNull(line -> {
                    try {
                        JsonNode node = mapper.readTree(line);
                        if (node.has("response")) {
                            return node.get("response").asText();
                        }
                        if (node.path("done").asBoolean(false)) {
                            return null; // Signal end
                        }
                    } catch (Exception e) {
                        log.trace("Parse error: {}", e.getMessage());
                    }
                    return null;
                })
                .takeUntil(token -> {
                    // Stop if we hit a stop token
                    if (stopTokens == null) return false;
                    for (String stop : stopTokens) {
                        if (token.contains(stop)) return true;
                    }
                    return false;
                })
                .doOnCancel(() -> log.debug("Completion request cancelled (user typed ahead)"))
                .doOnError(e -> log.warn("Ollama streaming error: {}", e.getMessage()));
    }
}
