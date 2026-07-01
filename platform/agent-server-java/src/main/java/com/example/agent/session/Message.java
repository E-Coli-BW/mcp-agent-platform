package com.example.agent.session;

/**
 * Immutable session message.
 */
public record Message(String role, String content, long timestamp) {

    /**
     * Create a message with the current timestamp.
     *
     * @param role message role
     * @param content message content
     */
    public Message(String role, String content) {
        this(role, content, System.currentTimeMillis());
    }
}
