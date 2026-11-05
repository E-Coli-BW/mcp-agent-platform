package com.example.agent;

import com.example.agent.config.AgentProperties;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class AgentConfigurationContractTest {

    @Test
    void should_notAdvertiseUnsupportedAgentConfiguration() {
        assertThrows(NoSuchMethodException.class,
                () -> AgentProperties.class.getDeclaredMethod("strongModel"));
        assertThrows(NoSuchMethodException.class,
                () -> AgentProperties.class.getDeclaredMethod("cheapModel"));
        assertThrows(NoSuchMethodException.class,
                () -> AgentProperties.class.getDeclaredMethod("configDir"));
        assertThrows(ClassNotFoundException.class,
                () -> Class.forName("com.example.agent.config.AgentConfigLoader"));
        assertNull(getClass().getClassLoader().getResource("agents/coding-agent.yaml"));
    }
}
