package com.example.common.spi;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Nacos-based service discovery implementation.
 *
 * Features:
 * - Register local service instance with Nacos (ephemeral)
 * - Heartbeat to keep registration alive (5s interval)
 * - Resolve service name → host:port via Nacos HTTP API
 * - Client-side cache with 10s TTL to reduce Nacos calls
 * - Graceful degradation: if Nacos unreachable, returns cached value
 */
public class NacosDiscovery implements ServiceDiscovery {

    private final String nacosUrl;
    private final HttpClient httpClient;
    private final ScheduledExecutorService scheduler;
    private final ConcurrentHashMap<String, CachedEndpoint> cache = new ConcurrentHashMap<>();

    private static final long CACHE_TTL_MS = 10_000;
    private static final long HEARTBEAT_INTERVAL_S = 5;

    record CachedEndpoint(String address, long timestamp) {
        boolean isExpired() {
            return System.currentTimeMillis() - timestamp > CACHE_TTL_MS;
        }
    }

    public NacosDiscovery() {
        this(System.getenv().getOrDefault("NACOS_URL", "http://localhost:8848"));
    }

    public NacosDiscovery(String nacosUrl) {
        this.nacosUrl = nacosUrl;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(3))
                .build();
        this.scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "nacos-heartbeat");
            t.setDaemon(true);
            return t;
        });
    }

    /**
     * Register a service instance with Nacos and start heartbeat.
     */
    public void register(String serviceName, String ip, int port) {
        doRegister(serviceName, ip, port);
        scheduler.scheduleAtFixedRate(
                () -> sendHeartbeat(serviceName, ip, port),
                HEARTBEAT_INTERVAL_S, HEARTBEAT_INTERVAL_S, TimeUnit.SECONDS
        );
    }

    /**
     * Resolve a service name to its address (host:port).
     * Uses client-side cache with TTL. Falls back to cached value on Nacos failure.
     */
    @Override
    public String resolve(String serviceName) {
        CachedEndpoint cached = cache.get(serviceName);
        if (cached != null && !cached.isExpired()) {
            return cached.address();
        }

        try {
            String address = queryNacos(serviceName);
            if (address != null) {
                cache.put(serviceName, new CachedEndpoint(address, System.currentTimeMillis()));
                return address;
            }
        } catch (Exception e) {
            // Graceful degradation: return stale cache if available
            if (cached != null) {
                return cached.address();
            }
        }

        return null;
    }

    /**
     * Query Nacos for healthy instances of a service.
     * Uses Nacos Open API v1: GET /nacos/v1/ns/instance/list
     */
    private String queryNacos(String serviceName) throws IOException, InterruptedException {
        String url = nacosUrl + "/nacos/v1/ns/instance/list?serviceName="
                + URLEncoder.encode(serviceName, StandardCharsets.UTF_8)
                + "&healthyOnly=true";

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .timeout(Duration.ofSeconds(3))
                .GET()
                .build();

        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        if (response.statusCode() != 200) {
            return null;
        }

        // Parse response: extract first healthy host from JSON
        // Response format: { "hosts": [{ "ip": "...", "port": N, "healthy": true }] }
        return parseFirstHealthyHost(response.body());
    }

    /**
     * Minimal JSON parse — avoids Jackson dependency in SPI module.
     * Extracts first "ip" and "port" from Nacos instance list response.
     */
    String parseFirstHealthyHost(String json) {
        // Find "hosts":[...] array, then extract first ip+port
        int hostsIdx = json.indexOf("\"hosts\"");
        if (hostsIdx < 0) return null;

        int ipIdx = json.indexOf("\"ip\"", hostsIdx);
        if (ipIdx < 0) return null;

        String ip = extractStringValue(json, ipIdx);
        if (ip == null) return null;

        int portIdx = json.indexOf("\"port\"", ipIdx);
        if (portIdx < 0) return null;

        int port = extractIntValue(json, portIdx);
        if (port <= 0) return null;

        return "http://" + ip + ":" + port;
    }

    private String extractStringValue(String json, int keyStart) {
        int colonIdx = json.indexOf(':', keyStart);
        if (colonIdx < 0) return null;
        int quoteStart = json.indexOf('"', colonIdx);
        if (quoteStart < 0) return null;
        int quoteEnd = json.indexOf('"', quoteStart + 1);
        if (quoteEnd < 0) return null;
        return json.substring(quoteStart + 1, quoteEnd);
    }

    private int extractIntValue(String json, int keyStart) {
        int colonIdx = json.indexOf(':', keyStart);
        if (colonIdx < 0) return -1;
        int start = colonIdx + 1;
        while (start < json.length() && !Character.isDigit(json.charAt(start))) start++;
        int end = start;
        while (end < json.length() && Character.isDigit(json.charAt(end))) end++;
        if (start == end) return -1;
        return Integer.parseInt(json.substring(start, end));
    }

    private void doRegister(String serviceName, String ip, int port) {
        String body = "serviceName=" + URLEncoder.encode(serviceName, StandardCharsets.UTF_8)
                + "&ip=" + URLEncoder.encode(ip, StandardCharsets.UTF_8)
                + "&port=" + port
                + "&ephemeral=true"
                + "&healthy=true";

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(nacosUrl + "/nacos/v1/ns/instance"))
                .timeout(Duration.ofSeconds(3))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

        try {
            HttpResponse<String> resp = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() != 200) {
                System.err.println("[NacosDiscovery] Registration failed: " + resp.body());
            }
        } catch (Exception e) {
            System.err.println("[NacosDiscovery] Registration error: " + e.getMessage());
        }
    }

    private void sendHeartbeat(String serviceName, String ip, int port) {
        String beat = "{\"serviceName\":\"" + serviceName + "\","
                + "\"ip\":\"" + ip + "\","
                + "\"port\":" + port + ","
                + "\"ephemeral\":true}";

        String url = nacosUrl + "/nacos/v1/ns/instance/beat"
                + "?serviceName=" + URLEncoder.encode(serviceName, StandardCharsets.UTF_8)
                + "&beat=" + URLEncoder.encode(beat, StandardCharsets.UTF_8);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .timeout(Duration.ofSeconds(3))
                .PUT(HttpRequest.BodyPublishers.noBody())
                .build();

        try {
            httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            // Heartbeat failure is non-fatal; Nacos will deregister after 15s
            System.err.println("[NacosDiscovery] Heartbeat failed: " + e.getMessage());
        }
    }

    /**
     * Shutdown scheduler gracefully.
     */
    public void shutdown() {
        scheduler.shutdownNow();
    }
}
