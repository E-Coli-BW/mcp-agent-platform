package com.example.agent.agent;

import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Explicit agent state for the streaming ReAct loop.
 *
 * <p>Separates concerns that the previous blocking ChatClient.call() conflated:
 * <ul>
 *   <li>{@code messages} — the full conversation history (grows with each tool round)</li>
 *   <li>{@code workspaceContext} — injected once per session, not re-read every LLM call</li>
 *   <li>{@code loopCounter} — tracks ReAct iterations to enforce max_steps</li>
 *   <li>{@code consecutiveErrors} — typed error tracking for recovery hints</li>
 * </ul>
 *
 * <p>Immutable by construction — each mutation returns a new instance. This mirrors the
 * Python v2 graph's frozen dataclass pattern: mutating shared state across reactive
 * threads is the bug factory we're avoiding.</p>
 */
public record AgentState(
        List<Message> messages,
        String workspaceContext,
        String skillsCatalog,
        int loopCounter,
        int consecutiveErrors,
        int critiqueAttempts
) {

    /**
     * Create a fresh state for a new request.
     *
     * @param userMessage the user's input message
     * @param systemPrompt full system prompt (with workspace context)
     * @return initial state
     */
    public static AgentState initial(String userMessage, String systemPrompt) {
        List<Message> msgs = new ArrayList<>();
        msgs.add(new SystemMessage(systemPrompt));
        msgs.add(new UserMessage(userMessage));
        return new AgentState(Collections.unmodifiableList(msgs), "", "", 0, 0, 0);
    }

    /**
     * Append a message and return a new state.
     */
    public AgentState withMessage(Message message) {
        List<Message> updated = new ArrayList<>(messages);
        updated.add(message);
        return new AgentState(Collections.unmodifiableList(updated), workspaceContext, skillsCatalog,
                loopCounter, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Append multiple messages and return a new state.
     */
    public AgentState withMessages(List<Message> newMessages) {
        List<Message> updated = new ArrayList<>(messages);
        updated.addAll(newMessages);
        return new AgentState(Collections.unmodifiableList(updated), workspaceContext, skillsCatalog,
                loopCounter, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Replace messages (used after compression).
     */
    public AgentState withReplacedMessages(List<Message> replacedMessages) {
        return new AgentState(Collections.unmodifiableList(replacedMessages), workspaceContext, skillsCatalog,
                loopCounter, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Increment the loop counter.
     */
    public AgentState incrementLoop() {
        return new AgentState(messages, workspaceContext, skillsCatalog,
                loopCounter + 1, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Update the consecutive error count.
     */
    public AgentState withConsecutiveErrors(int errors) {
        return new AgentState(messages, workspaceContext, skillsCatalog,
                loopCounter, errors, critiqueAttempts);
    }

    /**
     * Set workspace context.
     */
    public AgentState withWorkspaceContext(String ctx) {
        return new AgentState(messages, ctx, skillsCatalog,
                loopCounter, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Set skills catalog.
     */
    public AgentState withSkillsCatalog(String catalog) {
        return new AgentState(messages, workspaceContext, catalog,
                loopCounter, consecutiveErrors, critiqueAttempts);
    }

    /**
     * Get messages suitable for the LLM call (excluding system message,
     * which is handled by the prompt builder).
     */
    public List<Message> conversationMessages() {
        // Return all messages after the system message
        if (messages.isEmpty()) {
            return List.of();
        }
        if (messages.get(0) instanceof SystemMessage) {
            return messages.subList(1, messages.size());
        }
        return messages;
    }

    /**
     * Total character count across all messages (for budget tracking).
     */
    public int totalChars() {
        int total = 0;
        for (Message msg : messages) {
            String content = msg.getText();
            if (content != null) {
                total += content.length();
            }
        }
        return total;
    }
}

