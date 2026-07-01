package com.example.modelrouter.provider.ollama;

import com.example.modelrouter.provider.*;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import okhttp3.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Ollama provider — local LLM, free, no API key needed.
 * Default provider for MVP.
 */
@Component
@ConditionalOnProperty(name = "model-router.providers.ollama.enabled", havingValue = "true", matchIfMissing = true)
public class OllamaProvider implements LlmProvider {

    private static final Logger log = LoggerFactory.getLogger(OllamaProvider.class);
    private final OkHttpClient client;
    private final ObjectMapper mapper = new ObjectMapper();
    private final String baseUrl;
    private final String defaultModel;

    public OllamaProvider(
            @Value("${model-router.providers.ollama.base-url:http://localhost:11434}") String baseUrl,
            @Value("${model-router.providers.ollama.default-model:llama3.2}") String defaultModel,
            @Value("${model-router.timeout-seconds:30}") int timeout) {
        this.baseUrl = baseUrl;
        this.defaultModel = defaultModel;
        this.client = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(timeout, TimeUnit.SECONDS)
                .build();
    }

    @Override
    public String name() { return "ollama"; }

    @Override
    public boolean isAvailable() {
        try {
            Request req = new Request.Builder().url(baseUrl + "/api/tags").get().build();
            try (Response resp = client.newCall(req).execute()) {
                return resp.isSuccessful();
            }
        } catch (Exception e) {
            log.debug("Ollama not available: {}", e.getMessage());
            return false;
        }
    }

    @Override
    public LlmResponse complete(LlmRequest request) {
        String model = request.model() != null ? request.model() : defaultModel;
        long start = System.currentTimeMillis();

        try {
            var body = Map.of(
                    "model", model,
                    "prompt", request.prompt(),
                    "stream", false,
                    "options", Map.of(
                            "temperature", request.temperature() != null ? request.temperature() : 0.7,
                            "num_predict", request.maxTokens() != null ? request.maxTokens() : 4096
                    )
            );

            RequestBody reqBody = RequestBody.create(
                    mapper.writeValueAsString(body),
                    MediaType.get("application/json"));

            Request req = new Request.Builder()
                    .url(baseUrl + "/api/generate")
                    .post(reqBody)
                    .build();

            try (Response resp = client.newCall(req).execute()) {
                long duration = System.currentTimeMillis() - start;

                if (!resp.isSuccessful()) {
                    return LlmResponse.error("Ollama error: HTTP " + resp.code(), "ollama");
                }

                JsonNode json = mapper.readTree(resp.body().string());
                String content = json.path("response").asText("");
                int promptTokens = json.path("prompt_eval_count").asInt(0);
                int completionTokens = json.path("eval_count").asInt(0);

                return LlmResponse.of(content, model, "ollama", promptTokens, completionTokens, duration);
            }
        } catch (Exception e) {
            log.error("Ollama completion failed: {}", e.getMessage());
            return LlmResponse.error("Ollama error: " + e.getMessage(), "ollama");
        }
    }

    @Override
    public CostEstimate estimateCost(LlmRequest request) {
        return CostEstimate.usd(0, 0); // local = free
    }
}
