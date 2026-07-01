package com.example.memorystore.spi.impl;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for HttpEsOperations JSON response parsing.
 * Tests the parsing logic without needing a running Elasticsearch instance.
 */
class HttpEsOperationsTest {

    private final HttpEsOperations ops = new HttpEsOperations("http://localhost:9200");

    @Test
    void should_extractDocumentIds_when_validSearchResponse() {
        String response = """
            {
              "took": 5,
              "hits": {
                "total": {"value": 3},
                "hits": [
                  {"_index": "memory-tenant1", "_id": "key-alpha", "_score": 1.0},
                  {"_index": "memory-tenant1", "_id": "key-beta", "_score": 0.8},
                  {"_index": "memory-tenant1", "_id": "key-gamma", "_score": 0.5}
                ]
              }
            }
            """;

        List<String> ids = ops.extractIds(response);
        assertEquals(3, ids.size());
        assertEquals("key-alpha", ids.get(0));
        assertEquals("key-beta", ids.get(1));
        assertEquals("key-gamma", ids.get(2));
    }

    @Test
    void should_returnEmptyList_when_noHits() {
        String response = """
            {
              "took": 1,
              "hits": {
                "total": {"value": 0},
                "hits": []
              }
            }
            """;

        List<String> ids = ops.extractIds(response);
        assertTrue(ids.isEmpty());
    }

    @Test
    void should_handleSpecialCharsInId_when_parsing() {
        String response = """
            {
              "hits": {
                "hits": [
                  {"_id": "skill-maven-stale-jar-fix", "_score": 1.0}
                ]
              }
            }
            """;

        List<String> ids = ops.extractIds(response);
        assertEquals(1, ids.size());
        assertEquals("skill-maven-stale-jar-fix", ids.get(0));
    }

    @Test
    void should_returnEmptyList_when_malformedResponse() {
        List<String> ids = ops.extractIds("not json");
        assertTrue(ids.isEmpty());
    }
}
