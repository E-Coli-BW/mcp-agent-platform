package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.prompt.Prompt;

import java.time.Duration;
import java.util.concurrent.TimeoutException;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class AgentLoopTimeoutTest {

    @Test
    void should_cancelBlockingModelCallAtConfiguredDeadline() throws Exception {
        AgentProperties properties = new AgentProperties();
        properties.setLlmTimeout(1);
        ChatModel model = mock(ChatModel.class);
        when(model.call(any(Prompt.class))).thenAnswer(invocation -> {
            Thread.sleep(10_000);
            return null;
        });
        AgentLoopService service = new AgentLoopService(
                null, null, properties, null, null, null);

        assertTimeoutPreemptively(Duration.ofSeconds(2), () ->
                assertThrows(
                        TimeoutException.class,
                        () -> service.callModelWithTimeout(model, new Prompt("test"))));
    }
}
