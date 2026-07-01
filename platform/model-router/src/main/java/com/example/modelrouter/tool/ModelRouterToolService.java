package com.example.modelrouter.tool;

import com.example.mcp.common.security.TenantContext;
import com.example.mcp.common.util.ResultTruncator;
import com.example.modelrouter.provider.LlmRequest;
import com.example.modelrouter.provider.LlmResponse;
import com.example.modelrouter.service.ModelRouterService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class ModelRouterToolService {

    private static final Logger log = LoggerFactory.getLogger(ModelRouterToolService.class);
    private final ModelRouterService router;
    private final ObjectMapper mapper = new ObjectMapper();

    public ModelRouterToolService(ModelRouterService router) {
        this.router = router;
    }

    @Tool(description = "Complete text or code using the best available LLM. Supports multiple providers (Ollama local, OpenAI, Anthropic).")
    public String llm_complete(
            @ToolParam(description = "The prompt to complete") String prompt,
            @ToolParam(description = "Preferred provider: ollama, openai, anthropic", required = false) String provider,
            @ToolParam(description = "Model name override", required = false) String model,
            @ToolParam(description = "Max tokens for response", required = false) Integer maxTokens) {
        try {
            String tid = TenantContext.get();
            log.info("llm_complete: tenant={}, provider={}, promptLength={}", tid, provider, prompt.length());

            LlmRequest request = new LlmRequest(prompt, null, model, maxTokens, null, null);
            LlmResponse response = router.complete(request, provider);

            return formatResponse(response);
        } catch (Exception e) {
            log.error("llm_complete failed", e);
            return "❌ LLM error: " + e.getMessage();
        }
    }

    @Tool(description = "Summarize text to a shorter form. Useful for fitting content into context windows.")
    public String llm_summarize(
            @ToolParam(description = "Text to summarize") String text,
            @ToolParam(description = "Target length hint (e.g., '3 sentences', '100 words')", required = false) String targetLength) {
        try {
            String hint = targetLength != null ? " Target length: " + targetLength + "." : "";
            String prompt = "Summarize the following text concisely." + hint + "\n\n" + text;

            LlmRequest request = LlmRequest.of(prompt, null, 1024);
            LlmResponse response = router.complete(request, null);

            return "📝 Summary (" + response.provider() + ", " + response.totalTokens() + " tokens):\n\n"
                    + response.content();
        } catch (Exception e) {
            log.error("llm_summarize failed", e);
            return "❌ Summarization error: " + e.getMessage();
        }
    }

    @Tool(description = "Explain code, error, or concept in natural language.")
    public String llm_explain(
            @ToolParam(description = "Code, error message, or concept to explain") String input,
            @ToolParam(description = "Context or additional instructions", required = false) String context) {
        try {
            String ctx = context != null ? "\n\nContext: " + context : "";
            String prompt = "Explain the following clearly and concisely:" + ctx + "\n\n" + input;

            LlmRequest request = LlmRequest.of(prompt, null, 2048);
            LlmResponse response = router.complete(request, null);

            return "💡 Explanation (" + response.provider() + "):\n\n" + response.content();
        } catch (Exception e) {
            log.error("llm_explain failed", e);
            return "❌ Explanation error: " + e.getMessage();
        }
    }

    @Tool(description = "List available LLM models, their providers, status, and estimated costs.")
    public String llm_models() {
        try {
            var models = router.listModels();
            return "🤖 Available models:\n\n" + toJson(models);
        } catch (Exception e) {
            return "❌ Error listing models: " + e.getMessage();
        }
    }

    private String formatResponse(LlmResponse r) {
        if (r.content().startsWith("❌") || r.content().contains("error")) {
            return r.content();
        }
        var sb = new StringBuilder();
        sb.append("✅ ").append(r.provider()).append("/").append(r.model());
        sb.append(" (").append(r.totalTokens()).append(" tokens, ").append(r.durationMs()).append("ms)");
        if (r.fromCache()) sb.append(" [cached]");
        sb.append("\n\n").append(ResultTruncator.truncate(r.content()));
        return sb.toString();
    }

    private String toJson(Object obj) {
        try { return mapper.writerWithDefaultPrettyPrinter().writeValueAsString(obj); }
        catch (Exception e) { return "{}"; }
    }
}
