package com.example.agent;

import com.example.agent.config.AgentConfigLoader;
import com.example.agent.config.AgentProperties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

class AgentConfigLoaderTest {

    @TempDir
    Path tempDir;

    @Test
    void should_loadYaml_when_validFile() throws IOException {
        Path config = tempDir.resolve("test-agent.yaml");
        Files.writeString(config, """
            id: test-agent
            name: \"Test Agent\"
            version: \"2.0\"
            model: gpt-4o
            tools:
              - file_read
              - file_write
            guardrails:
              max_tool_calls: 10
            """);

        Path workspace = tempDir.resolve("workspace");
        var props = new AgentProperties("qwen2.5:7b", "qwen2.5:7b", "qwen2.5:7b", "v2",
                tempDir.toString(), 3000, 20000, 20, workspace.toString(), "secret",
                "http://localhost:8180", "http://localhost:8380", "http://localhost:8090");
        var loader = new AgentConfigLoader(props);
        var configs = loader.loadAll(tempDir.toString());

        assertEquals(1, configs.size());
        var cfg = configs.get("test-agent");
        assertNotNull(cfg);
        assertEquals("Test Agent", cfg.name());
        assertEquals("gpt-4o", cfg.model());
        assertEquals(2, cfg.tools().size());
    }

    @Test
    void should_returnEmpty_when_dirNotExists() {
        Path missingDir = tempDir.resolve("missing-configs");
        var props = new AgentProperties("qwen2.5:7b", "qwen2.5:7b", "qwen2.5:7b", "v2",
                missingDir.toString(), 3000, 20000, 20, tempDir.toString(), "secret",
                "http://localhost:8180", "http://localhost:8380", "http://localhost:8090");
        var loader = new AgentConfigLoader(props);
        var configs = loader.loadAll(missingDir.toString());
        assertTrue(configs.isEmpty());
    }
}
