package com.example.agent.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.yaml.snakeyaml.Yaml;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.stream.Stream;

/**
 * Loads agent configuration files from YAML.
 */
@Component
public class AgentConfigLoader {

    private static final Logger log = LoggerFactory.getLogger(AgentConfigLoader.class);

    private final AgentProperties properties;
    private final Yaml yaml = new Yaml();

    public AgentConfigLoader(AgentProperties properties) {
        this.properties = properties;
    }

    /**
     * Loads every YAML config in the configured directory.
     */
    public Map<String, AgentConfig> loadAll() {
        return loadAll(properties.configDir());
    }

    /**
     * Loads every YAML config in the given directory.
     */
    public Map<String, AgentConfig> loadAll(String configDir) {
        Path dir = Path.of(configDir);
        if (!Files.isDirectory(dir)) {
            dir = Path.of("src/main/resources").resolve(configDir);
        }
        if (!Files.isDirectory(dir)) {
            return Map.of();
        }
        Map<String, AgentConfig> configs = new LinkedHashMap<>();
        try (Stream<Path> files = Files.list(dir)) {
            files.filter(Files::isRegularFile)
                    .filter(path -> {
                        String name = path.getFileName().toString().toLowerCase();
                        return name.endsWith(".yaml") || name.endsWith(".yml");
                    })
                    .sorted()
                    .forEach(path -> load(path.toString()).ifPresent(config -> configs.put(config.id(), config)));
        } catch (IOException e) {
            log.warn("Failed to load configs from {}: {}", dir, e.getMessage());
            return Map.of();
        }
        return configs;
    }

    /**
     * Loads a single YAML config.
     */
    public Optional<AgentConfig> load(String path) {
        Path file = Path.of(path);
        if (!Files.isRegularFile(file)) {
            return Optional.empty();
        }
        try (InputStream input = Files.newInputStream(file)) {
            Object loaded = yaml.load(input);
            if (!(loaded instanceof Map<?, ?> raw)) {
                return Optional.empty();
            }
            String defaultId = file.getFileName().toString().replaceFirst("\\.[^.]+$", "");
            return Optional.of(new AgentConfig(
                    raw.get("id") == null ? defaultId : String.valueOf(raw.get("id")),
                    raw.get("name") == null ? null : String.valueOf(raw.get("name")),
                    raw.get("version") == null ? null : String.valueOf(raw.get("version")),
                    raw.get("model") == null ? null : String.valueOf(raw.get("model")),
                    raw.get("prompt") == null ? null : String.valueOf(raw.get("prompt")),
                    toStringList(raw.get("tools")),
                    toObjectMap(raw.get("guardrails")),
                    toObjectMap(raw.get("routing"))));
        } catch (IOException | RuntimeException e) {
            log.warn("Failed to parse config {}: {}", file, e.getMessage());
            return Optional.empty();
        }
    }

    private List<String> toStringList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) {
            result.add(String.valueOf(item));
        }
        return result;
    }

    private Map<String, Object> toObjectMap(Object value) {
        if (!(value instanceof Map<?, ?> map)) {
            return Map.of();
        }
        Map<String, Object> result = new LinkedHashMap<>();
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            result.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        return result;
    }
}
