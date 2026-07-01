package com.example.reviewagent.agent;

import com.example.reviewagent.tool.FileTools;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.MessageChatMemoryAdvisor;
import org.springframework.ai.chat.memory.InMemoryChatMemory;
import org.springframework.stereotype.Service;

/**
 * Spring AI-based code review agent.
 *
 * ARCHITECTURE COMPARISON (Python vs Java):
 *   Python: LangGraph create_react_agent(model, tools, prompt=modifier)
 *   Java:   ChatClient.builder(model).defaultTools(...).defaultAdvisors(memory).build()
 *
 * The patterns map 1:1:
 *   - LangGraph tools          → Spring AI @Tool beans
 *   - prompt modifier          → ChatClient Advisors (MessageChatMemoryAdvisor)
 *   - MemorySaver checkpointer → InMemoryChatMemory
 *   - astream_events()         → ChatClient.prompt().stream().content()
 */
@Service
public class ReviewAgent {

    private static final Logger log = LoggerFactory.getLogger(ReviewAgent.class);

    private static final String SYSTEM_PROMPT = """
            You are a senior code reviewer. Your job is to:
            1. Use file_list to explore the project structure
            2. Use file_read to examine specific files
            3. Use file_search to find patterns across the codebase
            4. Provide actionable code review feedback

            Focus on:
            - Security issues (SQL injection, path traversal, hardcoded secrets)
            - Performance problems (N+1 queries, missing indexes, unbounded collections)
            - Design patterns (SOLID violations, god classes, missing abstractions)
            - Error handling (swallowed exceptions, missing validation)
            - Test coverage gaps

            Be specific: cite file names, line numbers, and suggest fixes.
            """;

    private final ChatClient chatClient;

    public ReviewAgent(ChatClient.Builder chatClientBuilder, FileTools fileTools) {
        this.chatClient = chatClientBuilder
                .defaultSystem(SYSTEM_PROMPT)
                .defaultTools(fileTools)
                .defaultAdvisors(new MessageChatMemoryAdvisor(new InMemoryChatMemory()))
                .build();
        log.info("ReviewAgent initialized with Spring AI ChatClient + {} tools", 3);
    }

    /**
     * Run a code review on the current workspace.
     *
     * @param userMessage The review request (e.g., "Review the security of this project")
     * @param conversationId Session ID for multi-turn conversation
     * @return The agent's review response
     */
    public String review(String userMessage, String conversationId) {
        log.info("Review request [{}]: {}", conversationId, userMessage);

        String response = chatClient.prompt()
                .user(userMessage)
                .advisors(a -> a.param("chat_memory_conversation_id", conversationId))
                .call()
                .content();

        log.info("Review complete [{}]: {} chars", conversationId, response != null ? response.length() : 0);
        return response;
    }

    /**
     * Stream a code review response token by token.
     */
    public reactor.core.publisher.Flux<String> reviewStream(String userMessage, String conversationId) {
        return chatClient.prompt()
                .user(userMessage)
                .advisors(a -> a.param("chat_memory_conversation_id", conversationId))
                .stream()
                .content();
    }
}
