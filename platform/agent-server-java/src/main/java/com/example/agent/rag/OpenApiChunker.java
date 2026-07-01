package com.example.agent.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.yaml.snakeyaml.Yaml;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Chunker for OpenAPI/Swagger specs — extracts endpoints and schemas as chunks.
 * Falls back to fixed-size chunking for non-OpenAPI YAML/JSON files.
 */
@Component
public class OpenApiChunker {

    private static final Logger log = LoggerFactory.getLogger(OpenApiChunker.class);

    private final FixedSizeChunker fixedSizeChunker;

    public OpenApiChunker(FixedSizeChunker fixedSizeChunker) {
        this.fixedSizeChunker = fixedSizeChunker;
    }

    /**
     * Chunk an OpenAPI spec or fall back to fixed-size for regular YAML/JSON.
     */
    @SuppressWarnings("unchecked")
    public List<CodeChunk> chunk(Path filePath) {
        try {
            String content = Files.readString(filePath);
            Instant modified = Files.getLastModifiedTime(filePath).toInstant();

            Yaml yaml = new Yaml();
            Object parsed = yaml.load(content);
            if (!(parsed instanceof Map<?, ?> spec)) {
                return fixedSizeChunker.chunk(filePath);
            }

            Map<String, Object> specMap = (Map<String, Object>) spec;
            if (!specMap.containsKey("openapi") && !specMap.containsKey("swagger")) {
                return fixedSizeChunker.chunk(filePath);
            }

            List<CodeChunk> chunks = new ArrayList<>();
            String filePathStr = filePath.toString();

            Object pathsObj = specMap.get("paths");
            if (pathsObj instanceof Map<?, ?> paths) {
                for (Map.Entry<?, ?> pathEntry : paths.entrySet()) {
                    String path = pathEntry.getKey().toString();
                    if (pathEntry.getValue() instanceof Map<?, ?> methods) {
                        for (Map.Entry<?, ?> methodEntry : methods.entrySet()) {
                            String method = methodEntry.getKey().toString().toUpperCase();
                            if (!List.of("GET", "POST", "PUT", "DELETE", "PATCH").contains(method)) {
                                continue;
                            }
                            Map<String, Object> operation = (Map<String, Object>) methodEntry.getValue();
                            String endpointContent = formatEndpoint(method, path, operation);
                            chunks.add(new CodeChunk(
                                    endpointContent, filePathStr, "openapi", "api_endpoint",
                                    method + " " + path, 1, 1, modified, null
                            ));
                        }
                    }
                }
            }

            Map<String, Object> schemas = extractSchemas(specMap);
            if (schemas != null) {
                for (Map.Entry<String, Object> schemaEntry : schemas.entrySet()) {
                    String schemaName = schemaEntry.getKey();
                    if (schemaEntry.getValue() instanceof Map<?, ?> schemaDef) {
                        String schemaContent = formatSchema(schemaName, (Map<String, Object>) schemaDef);
                        chunks.add(new CodeChunk(
                                schemaContent, filePathStr, "openapi", "api_schema",
                                "Schema: " + schemaName, 1, 1, modified, null
                        ));
                    }
                }
            }

            return chunks.isEmpty() ? fixedSizeChunker.chunk(filePath) : chunks;
        } catch (IOException e) {
            log.debug("Failed to read {}: {}", filePath, e.getMessage());
            return List.of();
        } catch (Exception e) {
            log.debug("Failed to parse {}: {}", filePath, e.getMessage());
            return fixedSizeChunker.chunk(filePath);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> extractSchemas(Map<String, Object> spec) {
        Object components = spec.get("components");
        if (components instanceof Map<?, ?> comp) {
            Object schemas = ((Map<String, Object>) comp).get("schemas");
            if (schemas instanceof Map<?, ?>) {
                return (Map<String, Object>) schemas;
            }
        }

        Object definitions = spec.get("definitions");
        if (definitions instanceof Map<?, ?>) {
            return (Map<String, Object>) definitions;
        }
        return null;
    }

    @SuppressWarnings("unchecked")
    private String formatEndpoint(String method, String path, Map<String, Object> operation) {
        StringBuilder sb = new StringBuilder();
        sb.append(method).append(" ").append(path).append("\n");
        if (operation.containsKey("summary")) {
            sb.append("Summary: ").append(operation.get("summary")).append("\n");
        }
        if (operation.containsKey("description")) {
            sb.append("Description: ").append(operation.get("description")).append("\n");
        }
        if (operation.containsKey("parameters")) {
            sb.append("Parameters:\n");
            List<Map<String, Object>> params = (List<Map<String, Object>>) operation.get("parameters");
            for (Map<String, Object> param : params) {
                sb.append("  - ").append(param.getOrDefault("name", "unknown"))
                        .append(" (").append(param.getOrDefault("in", "query")).append(")")
                        .append(": ").append(param.getOrDefault("description", "")).append("\n");
            }
        }
        return sb.toString();
    }

    private String formatSchema(String name, Map<String, Object> schema) {
        StringBuilder sb = new StringBuilder();
        sb.append("Schema: ").append(name).append("\n");
        if (schema.containsKey("description")) {
            sb.append("Description: ").append(schema.get("description")).append("\n");
        }
        if (schema.containsKey("type")) {
            sb.append("Type: ").append(schema.get("type")).append("\n");
        }
        Object properties = schema.get("properties");
        if (properties instanceof Map<?, ?> props) {
            sb.append("Properties:\n");
            for (Map.Entry<?, ?> entry : props.entrySet()) {
                sb.append("  - ").append(entry.getKey());
                if (entry.getValue() instanceof Map<?, ?> propDef) {
                    Object type = propDef.get("type");
                    if (type != null) {
                        sb.append(" (").append(type).append(")");
                    }
                    Object desc = propDef.get("description");
                    if (desc != null) {
                        sb.append(": ").append(desc);
                    }
                }
                sb.append("\n");
            }
        }
        return sb.toString();
    }
}
