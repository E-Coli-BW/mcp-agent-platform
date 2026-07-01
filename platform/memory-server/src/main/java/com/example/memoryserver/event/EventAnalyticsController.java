package com.example.memoryserver.event;

import org.springframework.boot.autoconfigure.condition.ConditionalOnBean;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * REST endpoint for Kafka event analytics.
 * GET /api/analytics — returns tool usage stats consumed from Kafka.
 */
@RestController
@ConditionalOnBean(ToolEventConsumer.class)
public class EventAnalyticsController {

    private final ToolEventConsumer consumer;

    public EventAnalyticsController(ToolEventConsumer consumer) {
        this.consumer = consumer;
    }

    @GetMapping("/api/analytics")
    public Map<String, Object> analytics() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("total_tool_calls", consumer.getTotalToolCalls());
        result.put("total_responses", consumer.getTotalResponses());
        result.put("total_tokens", consumer.getTotalTokens());
        result.put("tool_usage", consumer.getToolUsageCount().entrySet().stream()
                .collect(Collectors.toMap(Map.Entry::getKey, e -> e.getValue().get())));
        result.put("active_sessions", consumer.getSessionToolCount().size());
        return result;
    }
}
