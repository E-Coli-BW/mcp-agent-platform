package com.example.completion.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

import java.util.List;

@Configuration
@ConfigurationProperties(prefix = "completion")
public class CompletionProperties {

    private Ollama ollama = new Ollama();
    private Fim fim = new Fim();
    private Cache cache = new Cache();
    private Debounce debounce = new Debounce();

    // Getters/setters
    public Ollama getOllama() { return ollama; }
    public void setOllama(Ollama ollama) { this.ollama = ollama; }
    public Fim getFim() { return fim; }
    public void setFim(Fim fim) { this.fim = fim; }
    public Cache getCache() { return cache; }
    public void setCache(Cache cache) { this.cache = cache; }
    public Debounce getDebounce() { return debounce; }
    public void setDebounce(Debounce debounce) { this.debounce = debounce; }

    public static class Ollama {
        private String baseUrl = "http://localhost:11434";
        private String model = "qwen2.5-coder:7b";
        private int timeoutMs = 5000;

        public String getBaseUrl() { return baseUrl; }
        public void setBaseUrl(String baseUrl) { this.baseUrl = baseUrl; }
        public String getModel() { return model; }
        public void setModel(String model) { this.model = model; }
        public int getTimeoutMs() { return timeoutMs; }
        public void setTimeoutMs(int timeoutMs) { this.timeoutMs = timeoutMs; }
    }

    public static class Fim {
        private int maxPrefixLines = 50;
        private int maxSuffixLines = 20;
        private int maxTokens = 128;
        private double temperature = 0.2;
        private List<String> stopTokens = List.of("\n\n", "def ", "class ", "public ", "import ");

        public int getMaxPrefixLines() { return maxPrefixLines; }
        public void setMaxPrefixLines(int v) { this.maxPrefixLines = v; }
        public int getMaxSuffixLines() { return maxSuffixLines; }
        public void setMaxSuffixLines(int v) { this.maxSuffixLines = v; }
        public int getMaxTokens() { return maxTokens; }
        public void setMaxTokens(int v) { this.maxTokens = v; }
        public double getTemperature() { return temperature; }
        public void setTemperature(double v) { this.temperature = v; }
        public List<String> getStopTokens() { return stopTokens; }
        public void setStopTokens(List<String> v) { this.stopTokens = v; }
    }

    public static class Cache {
        private int maxSize = 2000;
        private int expireMinutes = 5;

        public int getMaxSize() { return maxSize; }
        public void setMaxSize(int v) { this.maxSize = v; }
        public int getExpireMinutes() { return expireMinutes; }
        public void setExpireMinutes(int v) { this.expireMinutes = v; }
    }

    public static class Debounce {
        private boolean enabled = true;

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean v) { this.enabled = v; }
    }
}
