package com.example.memoryserver.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for ToolEventConsumer — validates event parsing and analytics tracking.
 * Tests run WITHOUT Kafka (consumer methods are called directly with JSON strings).
 */
class ToolEventConsumerTest {

    private ToolEventConsumer consumer;
    private ObjectMapper mapper;

    @BeforeEach
    void setUp() {
        mapper = new ObjectMapper();
        consumer = new ToolEventConsumer(mapper);
    }

    @Test
    void toolStartEvent_incrementsCounters() {
        String json = """
                {
                    "event_id": "test-1",
                    "timestamp": "2026-05-19T12:00:00.000Z",
                    "session_id": "session-abc",
                    "event_type": "tool_start",
                    "tool_name": "file_read",
                    "tool_input": {"path": "src/main.py"},
                    "model": "qwen2.5:7b"
                }
                """;

        consumer.onToolEvent(json);

        assertEquals(1, consumer.getTotalToolCalls());
        assertEquals(1, consumer.getToolUsageCount().get("file_read").get());
        assertEquals(1, consumer.getSessionToolCount().get("session-abc").get());
    }

    @Test
    void multipleToolEvents_tracksPerTool() {
        String fileRead = """
                {"event_id":"1","timestamp":"","session_id":"s1","event_type":"tool_start","tool_name":"file_read","tool_input":{}}
                """;
        String fileList = """
                {"event_id":"2","timestamp":"","session_id":"s1","event_type":"tool_start","tool_name":"file_list","tool_input":{}}
                """;

        consumer.onToolEvent(fileRead);
        consumer.onToolEvent(fileRead);
        consumer.onToolEvent(fileList);

        assertEquals(3, consumer.getTotalToolCalls());
        assertEquals(2, consumer.getToolUsageCount().get("file_read").get());
        assertEquals(1, consumer.getToolUsageCount().get("file_list").get());
    }

    @Test
    void toolEndEvent_doesNotIncrementToolCalls() {
        String json = """
                {"event_id":"1","timestamp":"","session_id":"s1","event_type":"tool_end","tool_name":"file_read","duration_ms":42}
                """;

        consumer.onToolEvent(json);

        assertEquals(0, consumer.getTotalToolCalls());
    }

    @Test
    void agentResponse_tracksTokens() {
        String json = """
                {
                    "event_id": "resp-1",
                    "timestamp": "2026-05-19T12:00:00.000Z",
                    "session_id": "session-abc",
                    "event_type": "agent_response",
                    "model": "qwen2.5:7b",
                    "token_count": 500,
                    "duration_ms": 2000
                }
                """;

        consumer.onAgentResponse(json);

        assertEquals(1, consumer.getTotalResponses());
        assertEquals(500, consumer.getTotalTokens());
    }

    @Test
    void multipleResponses_accumulateTokens() {
        String json1 = """
                {"event_id":"1","timestamp":"","session_id":"s1","event_type":"agent_response","model":"qwen2.5:7b","token_count":300,"duration_ms":1000}
                """;
        String json2 = """
                {"event_id":"2","timestamp":"","session_id":"s2","event_type":"agent_response","model":"qwen2.5:7b","token_count":200,"duration_ms":500}
                """;

        consumer.onAgentResponse(json1);
        consumer.onAgentResponse(json2);

        assertEquals(2, consumer.getTotalResponses());
        assertEquals(500, consumer.getTotalTokens());
    }

    @Test
    void malformedJson_doesNotThrow() {
        assertDoesNotThrow(() -> consumer.onToolEvent("not valid json"));
        assertDoesNotThrow(() -> consumer.onAgentResponse("{invalid}}}"));
        assertEquals(0, consumer.getTotalToolCalls());
    }

    @Test
    void multipleSessions_trackedSeparately() {
        String s1 = """
                {"event_id":"1","timestamp":"","session_id":"session-1","event_type":"tool_start","tool_name":"file_read","tool_input":{}}
                """;
        String s2 = """
                {"event_id":"2","timestamp":"","session_id":"session-2","event_type":"tool_start","tool_name":"file_list","tool_input":{}}
                """;

        consumer.onToolEvent(s1);
        consumer.onToolEvent(s1);
        consumer.onToolEvent(s2);

        assertEquals(2, consumer.getSessionToolCount().get("session-1").get());
        assertEquals(1, consumer.getSessionToolCount().get("session-2").get());
    }

    @Test
    void eventRecordDeserialization() throws Exception {
        String json = """
                {
                    "event_id": "test-1",
                    "timestamp": "2026-05-19T12:00:00.000Z",
                    "session_id": "ide-abc123",
                    "event_type": "tool_start",
                    "tool_name": "rag_search",
                    "tool_input": {"query": "JWT auth", "top_k": 5},
                    "model": "qwen2.5:7b",
                    "duration_ms": 0,
                    "token_count": 0
                }
                """;

        ToolEventRecord record = mapper.readValue(json, ToolEventRecord.class);

        assertEquals("test-1", record.eventId());
        assertEquals("ide-abc123", record.sessionId());
        assertEquals("tool_start", record.eventType());
        assertEquals("rag_search", record.toolName());
        assertEquals("JWT auth", record.toolInput().get("query"));
    }
}
