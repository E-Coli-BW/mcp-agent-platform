package com.example.agent.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Generates embeddings via Ollama.
 */
@Component
public class OllamaEmbedder {

    private static final Logger log = LoggerFactory.getLogger(OllamaEmbedder.class);
    private static final int MAX_EMBED_CHARS = 1500;
    private static final int EMBED_DIM = 1024;
    private static final String EMBED_MODEL = "mxbai-embed-large";

    private final WebClient webClient;

    public OllamaEmbedder(@Value("${spring.ai.ollama.base-url:http://localhost:11434}") String baseUrl) {
        this.webClient = WebClient.builder().baseUrl(baseUrl).build();
    }

    /**
     * Embeds a single text input.
     */
    public Mono<float[]> embed(String text) {
        String cleaned = truncate(clean(text), MAX_EMBED_CHARS);
        if (cleaned.isBlank()) {
            return Mono.just(zeroVector());
        }
        return webClient.post()
                .uri("/api/embeddings")
                .bodyValue(Map.of("model", EMBED_MODEL, "prompt", cleaned))
                .retrieve()
                .bodyToMono(Map.class)
                .map(this::mapEmbedding)
                .timeout(Duration.ofSeconds(30))
                .onErrorResume(ex -> {
                    log.warn("Embedding failed, retrying with truncated input: {}", ex.getMessage());
                    return retryEmbed(truncate(cleaned, MAX_EMBED_CHARS / 2));
                });
    }

    /**
     * Embeds texts in batches with small pacing delays.
     */
    public Flux<float[]> embedBatch(List<String> texts, int batchSize) {
        return Flux.fromIterable(texts)
                .buffer(batchSize)
                .concatMap(batch -> Flux.fromIterable(batch)
                        .flatMap(this::embed)
                        .collectList()
                        .delayElement(Duration.ofMillis(100))
                        .flatMapMany(Flux::fromIterable));
    }

    private Mono<float[]> retryEmbed(String text) {
        return webClient.post()
                .uri("/api/embeddings")
                .bodyValue(Map.of("model", EMBED_MODEL, "prompt", text))
                .retrieve()
                .bodyToMono(Map.class)
                .map(this::mapEmbedding)
                .timeout(Duration.ofSeconds(30))
                .onErrorReturn(zeroVector());
    }

    private float[] mapEmbedding(Map<?, ?> response) {
        Object raw = response.get("embedding");
        if (!(raw instanceof List<?> embedding)) {
            return zeroVector();
        }
        float[] result = new float[embedding.size()];
        for (int i = 0; i < embedding.size(); i++) {
            Object value = embedding.get(i);
            if (value instanceof Number number) {
                result[i] = number.floatValue();
            }
        }
        return result;
    }

    private String clean(String text) {
        if (text == null) {
            return "";
        }
        return text.replaceAll("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]", "").replaceAll("\\s+", " ").trim();
    }

    private String truncate(String text, int maxChars) {
        return text.length() <= maxChars ? text : text.substring(0, maxChars);
    }

    private float[] zeroVector() {
        return new float[EMBED_DIM];
    }
}
