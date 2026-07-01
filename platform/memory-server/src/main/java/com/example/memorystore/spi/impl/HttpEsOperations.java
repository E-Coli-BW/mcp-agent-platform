package com.example.memorystore.spi.impl;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * Real Elasticsearch implementation of EsOperations using Java HttpClient.
 *
 * Connects to Elasticsearch REST API (default: http://localhost:9200).
 * Uses single-node mode (no cluster auth) for development.
 *
 * Index naming: memory-{tenantId} (auto-created on first write).
 *
 * Features:
 * - Index documents (PUT /{index}/_doc/{id})
 * - Get documents by ID (GET /{index}/_doc/{id})
 * - Delete documents (DELETE /{index}/_doc/{id})
 * - List all document IDs (POST /{index}/_search with match_all)
 * - Full-text search (POST /{index}/_search with multi_match)
 * - Auto-create index with proper text mappings on first write
 */
public class HttpEsOperations implements ElasticsearchMemoryBackend.EsOperations {

    private final String baseUrl;
    private final HttpClient httpClient;

    public HttpEsOperations() {
        this(System.getenv().getOrDefault("ELASTICSEARCH_URL", "http://localhost:9200"));
    }

    public HttpEsOperations(String baseUrl) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();
    }

    @Override
    public void index(String indexName, String id, String jsonBody) {
        ensureIndex(indexName);

        // Wrap raw content into a document with searchable fields
        String docBody = """
            {"key": "%s", "content": %s, "indexed_at": "%s"}
            """.formatted(
                escapeJson(id),
                jsonBody.startsWith("{") ? jsonBody : "\"" + escapeJson(jsonBody) + "\"",
                java.time.Instant.now().toString()
        );

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName + "/_doc/" + urlEncode(id)))
                .header("Content-Type", "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(docBody))
                .timeout(Duration.ofSeconds(5))
                .build();

        sendRequest(request);
    }

    @Override
    public String get(String indexName, String id) {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName + "/_doc/" + urlEncode(id)))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 404) {
                return null;
            }
            if (response.statusCode() != 200) {
                return null;
            }
            // Extract _source.content from response
            return extractField(response.body(), "content");
        } catch (IOException | InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        }
    }

    @Override
    public boolean delete(String indexName, String id) {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName + "/_doc/" + urlEncode(id)))
                .timeout(Duration.ofSeconds(5))
                .DELETE()
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            return response.statusCode() == 200;
        } catch (IOException | InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    @Override
    public List<String> listIds(String indexName) {
        String query = """
            {"query": {"match_all": {}}, "_source": false, "size": 1000}
            """;

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName + "/_search"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(query))
                .timeout(Duration.ofSeconds(10))
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 404) {
                return List.of(); // Index doesn't exist yet
            }
            if (response.statusCode() != 200) {
                return List.of();
            }
            return extractIds(response.body());
        } catch (IOException | InterruptedException e) {
            Thread.currentThread().interrupt();
            return List.of();
        }
    }

    @Override
    public List<String> search(String indexName, String query) {
        String searchBody = """
            {
              "query": {
                "multi_match": {
                  "query": "%s",
                  "fields": ["content", "key"],
                  "type": "best_fields",
                  "fuzziness": "AUTO"
                }
              },
              "size": 50,
              "_source": ["key", "content"]
            }
            """.formatted(escapeJson(query));

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName + "/_search"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(searchBody))
                .timeout(Duration.ofSeconds(10))
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 404) {
                return List.of();
            }
            if (response.statusCode() != 200) {
                return List.of();
            }
            return extractSearchResults(response.body());
        } catch (IOException | InterruptedException e) {
            Thread.currentThread().interrupt();
            return List.of();
        }
    }

    // ─── Index Management ──────────────────────────────────────────────

    private void ensureIndex(String indexName) {
        HttpRequest checkRequest = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName))
                .method("HEAD", HttpRequest.BodyPublishers.noBody())
                .timeout(Duration.ofSeconds(3))
                .build();

        try {
            HttpResponse<Void> response = httpClient.send(checkRequest, HttpResponse.BodyHandlers.discarding());
            if (response.statusCode() == 404) {
                createIndex(indexName);
            }
        } catch (IOException | InterruptedException e) {
            // Best-effort; ES will auto-create with dynamic mapping anyway
            Thread.currentThread().interrupt();
        }
    }

    private void createIndex(String indexName) {
        String mapping = """
            {
              "mappings": {
                "properties": {
                  "key": { "type": "keyword" },
                  "content": { "type": "text", "analyzer": "standard" },
                  "indexed_at": { "type": "date" }
                }
              },
              "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0
              }
            }
            """;

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/" + indexName))
                .header("Content-Type", "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(mapping))
                .timeout(Duration.ofSeconds(5))
                .build();

        sendRequest(request);
    }

    // ─── Response Parsing (minimal JSON, no Jackson dependency) ────────

    /**
     * Extract "content" field from ES _source response.
     * Response format: {..., "_source": {"key": "...", "content": "..."}}
     */
    private String extractField(String json, String field) {
        String sourceKey = "\"_source\"";
        int sourceIdx = json.indexOf(sourceKey);
        if (sourceIdx < 0) return null;

        String fieldKey = "\"" + field + "\"";
        int fieldIdx = json.indexOf(fieldKey, sourceIdx);
        if (fieldIdx < 0) return null;

        int colonIdx = json.indexOf(':', fieldIdx);
        if (colonIdx < 0) return null;

        // Skip whitespace after colon
        int valueStart = colonIdx + 1;
        while (valueStart < json.length() && Character.isWhitespace(json.charAt(valueStart))) {
            valueStart++;
        }

        if (valueStart >= json.length()) return null;

        char firstChar = json.charAt(valueStart);
        if (firstChar == '"') {
            // String value
            int quoteEnd = findClosingQuote(json, valueStart + 1);
            if (quoteEnd < 0) return null;
            return json.substring(valueStart + 1, quoteEnd);
        } else if (firstChar == '{') {
            // Object value — return as-is
            int braceEnd = findClosingBrace(json, valueStart);
            if (braceEnd < 0) return null;
            return json.substring(valueStart, braceEnd + 1);
        }
        return null;
    }

    /**
     * Extract document IDs from search response.
     * Response: {..., "hits": {"hits": [{"_id": "key1"}, {"_id": "key2"}]}}
     */
    List<String> extractIds(String json) {
        List<String> ids = new ArrayList<>();
        int searchFrom = 0;
        while (true) {
            int idIdx = json.indexOf("\"_id\"", searchFrom);
            if (idIdx < 0) break;
            int colonIdx = json.indexOf(':', idIdx);
            if (colonIdx < 0) break;
            int quoteStart = json.indexOf('"', colonIdx);
            if (quoteStart < 0) break;
            int quoteEnd = findClosingQuote(json, quoteStart + 1);
            if (quoteEnd < 0) break;
            ids.add(json.substring(quoteStart + 1, quoteEnd));
            searchFrom = quoteEnd + 1;
        }
        return ids;
    }

    /**
     * Extract content from search hits for search results.
     */
    private List<String> extractSearchResults(String json) {
        List<String> results = new ArrayList<>();
        String sourceKey = "\"_source\"";
        int searchFrom = 0;
        while (true) {
            int sourceIdx = json.indexOf(sourceKey, searchFrom);
            if (sourceIdx < 0) break;
            int braceStart = json.indexOf('{', sourceIdx + sourceKey.length());
            if (braceStart < 0) break;
            int braceEnd = findClosingBrace(json, braceStart);
            if (braceEnd < 0) break;
            results.add(json.substring(braceStart, braceEnd + 1));
            searchFrom = braceEnd + 1;
        }
        return results;
    }

    private int findClosingQuote(String json, int start) {
        for (int i = start; i < json.length(); i++) {
            if (json.charAt(i) == '"' && json.charAt(i - 1) != '\\') {
                return i;
            }
        }
        return -1;
    }

    private int findClosingBrace(String json, int start) {
        int depth = 0;
        for (int i = start; i < json.length(); i++) {
            char c = json.charAt(i);
            if (c == '{') depth++;
            else if (c == '}') {
                depth--;
                if (depth == 0) return i;
            }
        }
        return -1;
    }

    // ─── Helpers ───────────────────────────────────────────────────────

    private void sendRequest(HttpRequest request) {
        try {
            httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException | InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static String urlEncode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static String escapeJson(String value) {
        return value.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
