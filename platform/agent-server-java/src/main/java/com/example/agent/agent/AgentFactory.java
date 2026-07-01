package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import com.example.agent.tools.ToolRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.ollama.OllamaChatModel;
import org.springframework.ai.ollama.api.OllamaApi;
import org.springframework.ai.ollama.api.OllamaChatOptions;
import org.springframework.ai.openai.OpenAiChatModel;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.api.OpenAiApi;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.stereotype.Service;

import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Creates and caches ChatClient/ChatModel instances per model name.
 *
 * <p>Supports Ollama (default), OpenAI ("openai/gpt-4o"), and Anthropic
 * ("anthropic/claude-...") via the OpenAI-compatible endpoint.</p>
 *
 * <p>Exposes both the legacy {@link #getClient(String)} for backward compat
 * and the new {@link #getChatModel(String, Double)} for the streaming agent loop.</p>
 */
@Service
public class AgentFactory {

    private static final Logger log = LoggerFactory.getLogger(AgentFactory.class);

    private final AgentProperties properties;
    private final PromptService promptService;
    private final ToolRegistry toolRegistry;
    private final Map<String, ChatClient> clients = new ConcurrentHashMap<>();
    private final Map<String, ChatModel> chatModels = new ConcurrentHashMap<>();

    public AgentFactory(AgentProperties properties, PromptService promptService, ToolRegistry toolRegistry) {
        this.properties = properties;
        this.promptService = promptService;
        this.toolRegistry = toolRegistry;
    }

    /**
     * Get or create a ChatClient for the given model (legacy API).
     */
    public ChatClient getClient(String modelName) {
        String key = modelName == null || modelName.isBlank() || "coding-agent".equals(modelName)
                ? properties.defaultModel()
                : modelName;
        return clients.computeIfAbsent(key, this::createClient);
    }

    /**
     * Get or create a ChatModel for the streaming agent loop.
     *
     * <p>The cache key includes temperature because different temperature values
     * require different model instances (ChatModel bakes sampling params in).</p>
     *
     * @param modelName model name (null or "coding-agent" → default)
     * @param temperature override (null → use properties.defaultTemperature 0.7)
     * @return the ChatModel instance
     */
    public ChatModel getChatModel(String modelName, Double temperature) {
        String resolvedName = modelName == null || modelName.isBlank() || "coding-agent".equals(modelName)
                ? properties.defaultModel()
                : modelName;
        double resolvedTemp = temperature != null ? temperature : 0.7;
        String key = resolvedName + "@T=" + resolvedTemp;
        return chatModels.computeIfAbsent(key, k -> createChatModel(resolvedName, resolvedTemp));
    }

    /**
     * Return all registered tool callbacks as an array.
     */
    public ToolCallback[] getToolCallbacks() {
        return toolRegistry.getAllToolCallbacks();
    }

    /**
     * Build provider-specific prompt options with tool callbacks included.
     *
     * <p>This creates the correct option type for the model provider (OllamaChatOptions,
     * OpenAiChatOptions, etc.) so that Spring AI's buildRequestPrompt takes the optimized
     * path that correctly preserves ToolCallback references — avoiding the generic
     * copyToTarget path that may lose functional objects during Jackson serialization.</p>
     *
     * @param modelName the model name (to determine provider)
     * @param tools the tool callbacks to include
     * @return provider-specific ChatOptions with tools and internalToolExecutionEnabled=false
     */
    public ChatOptions buildPromptOptions(String modelName, ToolCallback[] tools) {
        String resolvedName = modelName == null || modelName.isBlank() || "coding-agent".equals(modelName)
                ? properties.defaultModel()
                : modelName;

        var toolList = Arrays.asList(tools);

        if (resolvedName.startsWith("openai/") || resolvedName.startsWith("gpt-")) {
            return OpenAiChatOptions.builder()
                    .toolCallbacks(toolList)
                    .internalToolExecutionEnabled(false)
                    .build();
        }

        // Default: Ollama — use OllamaChatOptions to take the instanceof-optimized path
        return OllamaChatOptions.builder()
                .toolCallbacks(toolList)
                .internalToolExecutionEnabled(false)
                .build();
    }

    /**
     * Build the full system prompt (with workspace context).
     */
    public String buildSystemPrompt() {
        return promptService.buildFullPrompt(null);
    }

    /**
     * Clear all cached instances (called on config hot-reload).
     */
    public void clearCache() {
        clients.clear();
        chatModels.clear();
        log.info("🔄 Agent client cache cleared");
    }

    private ChatClient createClient(String modelName) {
        ChatModel model = createChatModel(modelName, 0.7);
        String systemPrompt = promptService.buildFullPrompt(null);
        var toolCallbacks = toolRegistry.getAllToolCallbacks();

        log.info("Creating ChatClient for model={} with {} tools", modelName, toolCallbacks.length);
        return ChatClient.builder(model)
                .defaultSystem(systemPrompt)
                .defaultTools(toolCallbacks)
                .build();
    }

    private ChatModel createChatModel(String name, double temperature) {
        if (name.startsWith("openai/") || name.startsWith("gpt-")) {
            String modelId = name.replace("openai/", "");
            String apiKey = properties.openaiApiKey();
            if (apiKey == null || apiKey.isBlank()) {
                throw new IllegalStateException("AGENT_OPENAI_API_KEY not set for model: " + name);
            }
            var api = OpenAiApi.builder().apiKey(apiKey).build();
            var options = OpenAiChatOptions.builder().model(modelId).temperature(temperature).build();
            return OpenAiChatModel.builder().openAiApi(api).defaultOptions(options).build();
        }

        if (name.startsWith("anthropic/") || name.startsWith("claude")) {
            String modelId = name.replace("anthropic/", "");
            String apiKey = properties.anthropicApiKey();
            if (apiKey == null || apiKey.isBlank()) {
                throw new IllegalStateException("AGENT_ANTHROPIC_API_KEY not set for model: " + name);
            }
            var api = OpenAiApi.builder()
                    .apiKey(apiKey)
                    .baseUrl("https://api.anthropic.com/v1")
                    .build();
            var options = OpenAiChatOptions.builder().model(modelId).temperature(temperature).build();
            return OpenAiChatModel.builder().openAiApi(api).defaultOptions(options).build();
        }

        // Default: Ollama
        String modelId = name.replace("ollama/", "");
        var api = OllamaApi.builder().baseUrl(properties.ollamaBaseUrl()).build();
        var tools = toolRegistry.getAllTools();
        var options = OllamaChatOptions.builder()
                .model(modelId)
                .temperature(temperature)
                .toolCallbacks(tools)
                .internalToolExecutionEnabled(false)
                .build();
        log.info("🔧 Creating OllamaChatModel for model={} with {} tools in defaultOptions",
                modelId, tools.size());
        return OllamaChatModel.builder().ollamaApi(api).defaultOptions(options).build();
    }
}
