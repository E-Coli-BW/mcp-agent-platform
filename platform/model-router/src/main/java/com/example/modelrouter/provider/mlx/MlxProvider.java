package com.example.modelrouter.provider.mlx;

import com.example.modelrouter.provider.*;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * MLX provider — Apple Silicon native inference via OpenAI-compatible API.
 *
 * Runs on localhost:8600 (platform/llm-infra/serving/server.py).
 * Much faster than Ollama for small models (165 tok/s vs 37 tok/s on 0.5B).
 * Best for: latency-sensitive tasks where 0.5B quality suffices (code completion,
 * tool routing, reranking).
 */
@Component
@ConditionalOnProperty(name = "model-router.providers.mlx.enabled", havingValue = "true", matchIfMissing = false)
public class MlxProvider implements LlmProvider {

    private static final Logger log = LoggerFactory.getLogger(MlxProvider.class);
    private final OkHttpClient client;
    private final ObjectMapper mapper = new ObjectMapper();
    private final String baseUrl;
    private final String defaultModel;

    public MlxProvider(
            @Value("${model-router.providers.mlx.base-url:http://localhost:8600}") String baseUrl,
            @Value("${model-router.providers.mlx.default-model:Qwen2.5-0.5B-Instruct-4bit}") String defaultModel,
            @Value("${model-router.timeout-seconds:30}") int timeout) {
        this.baseUrl = baseUrl;
        this.defaultModel = defaultModel;
        this.client = new OkHttpClient.Builder()
                .connectTimeout(3, TimeUnit.SECONDS)
                .readTimeout(timeout, TimeUnit.SECONDS)
                .build();
    }

    @Override
    public String name() {
        return "mlx";
    }

    @Override
    public boolean isAvailable() {
        try {
            Request req = new Request.Builder().url(baseUrl + "/health").get().build();
            try (Response resp = client.newCall(req).execute()) {
                return resp.isSuccessful();
            }
        } catch (Exception e) {
            log.debug("MLX server not available: {}", e.getMessage());
            return false;
        }
    }

    @Override
    public LlmResponse complete(LlmRequest request) {
        String model = request.model() != null ? request.model() : defaultModel;
        long start = System.currentTimeMillis();

        try {
            // OpenAI-compatible /v1/chat/completions format
            var messages = List.of(Map.of("role", "user", "content", request.prompt()));
            var body = Map.of(
                    "model", model,
                    "messages", messages,
                    "stream", false,
                    "temperature", request.temperature() != null ? request.temperature() : 0.7,
                    "max_tokens", request.maxTokens() != null ? request.maxTokens() : 4096
            );

            RequestBody reqBody = RequestBody.create(
                    mapper.writeValueAsString(body),
                    MediaType.get("application/json"));

            Request req = new Request.Builder()
                    .url(baseUrl + "/v1/chat/completions")
                    .post(reqBody)
                    .build();

            try (Response resp = client.newCall(req).execute()) {
                long duration = System.currentTimeMillis() - start;

                if (!resp.isSuccessful()) {
                    return LlmResponse.error("MLX error: HTTP " + resp.code(), "mlx");
                }

                JsonNode json = mapper.readTree(resp.body().string());
                String content = json.path("choices").get(0)
                        .path("message").path("content").asText("");
                int promptTokens = json.path("usage").path("prompt_tokens").asInt(0);
                int completionTokens = json.path("usage").path("completion_tokens").asInt(0);

                return LlmResponse.of(content, model, "mlx", promptTokens, completionTokens, duration);
            }
        } catch (Exception e) {
            log.error("MLX completion failed: {}", e.getMessage());
            return LlmResponse.error("MLX error: " + e.getMessage(), "mlx");
        }
    }

    @Override
    public CostEstimate estimateCost(LlmRequest request) {
        return CostEstimate.usd(0, 0); // local = free
    }
}
