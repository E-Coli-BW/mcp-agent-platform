package com.example.memorystore.spi.bridge;

import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.search.Hit;
import com.example.memorystore.spi.impl.ElasticsearchMemoryBackend;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.StringReader;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Bridges the Elasticsearch Java API Client to our EsOperations SPI.
 * Use this in production Spring context to wire ElasticsearchMemoryBackend.
 */
public class SpringElasticsearchOperations implements ElasticsearchMemoryBackend.EsOperations {

    private static final Logger log = LoggerFactory.getLogger(SpringElasticsearchOperations.class);

    private final ElasticsearchClient client;

    public SpringElasticsearchOperations(ElasticsearchClient client) {
        this.client = client;
    }

    @Override
    public void index(String indexName, String id, String jsonBody) {
        try {
            client.index(i -> i.index(indexName).id(id).withJson(new StringReader(jsonBody)));
        } catch (Exception e) {
            throw new RuntimeException("ES index failed: " + e.getMessage(), e);
        }
    }

    @Override
    public String get(String indexName, String id) {
        try {
            var resp = client.get(g -> g.index(indexName).id(id), String.class);
            return resp.found() ? resp.source() : null;
        } catch (Exception e) {
            log.warn("ES get failed (index={}, id={}): {}", indexName, id, e.getMessage());
            return null;
        }
    }

    @Override
    public boolean delete(String indexName, String id) {
        try {
            var resp = client.delete(d -> d.index(indexName).id(id));
            return "deleted".equals(resp.result().jsonValue());
        } catch (Exception e) {
            log.warn("ES delete failed (index={}, id={}): {}", indexName, id, e.getMessage());
            return false;
        }
    }

    @Override
    public List<String> listIds(String indexName) {
        try {
            var resp = client.search(s -> s
                    .index(indexName)
                    .size(10000)
                    .source(sc -> sc.fetch(false)),
                    Void.class);
            return resp.hits().hits().stream()
                    .map(Hit::id)
                    .collect(Collectors.toList());
        } catch (Exception e) {
            log.warn("ES listIds failed (index={}): {}", indexName, e.getMessage());
            return List.of();
        }
    }

    @Override
    public List<String> search(String indexName, String query) {
        try {
            var resp = client.search(s -> s
                    .index(indexName)
                    .query(q -> q.queryString(qs -> qs.query(query)))
                    .size(100),
                    String.class);
            return resp.hits().hits().stream()
                    .map(Hit::source)
                    .collect(Collectors.toList());
        } catch (Exception e) {
            log.warn("ES search failed (index={}, query={}): {}", indexName, query, e.getMessage());
            return List.of();
        }
    }
}
