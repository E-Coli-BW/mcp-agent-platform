package com.example.agent.config;

import com.example.agent.agent.AgentFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.SmartLifecycle;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardWatchEventKinds;
import java.nio.file.WatchEvent;
import java.nio.file.WatchKey;
import java.nio.file.WatchService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

@Component
public class ConfigWatcher implements SmartLifecycle {

    private static final Logger log = LoggerFactory.getLogger(ConfigWatcher.class);

    private final AgentProperties properties;
    private final AgentFactory agentFactory;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private Thread watchThread;

    public ConfigWatcher(AgentProperties properties, AgentFactory agentFactory) {
        this.properties = properties;
        this.agentFactory = agentFactory;
    }

    @Override
    public void start() {
        if (running.compareAndSet(false, true)) {
            watchThread = new Thread(this::watchLoop, "config-watcher");
            watchThread.setDaemon(true);
            watchThread.start();
        }
    }

    @Override
    public void stop() {
        running.set(false);
        if (watchThread != null) {
            watchThread.interrupt();
        }
    }

    @Override
    public boolean isRunning() {
        return running.get();
    }

    private void watchLoop() {
        Path dir = Path.of(properties.configDir());
        if (!Files.exists(dir)) {
            dir = Path.of("src/main/resources").resolve(properties.configDir());
        }
        if (!Files.exists(dir)) {
            log.info("Config dir does not exist, skipping watch: {}", properties.configDir());
            return;
        }

        try (WatchService watcher = FileSystems.getDefault().newWatchService()) {
            dir.register(watcher,
                    StandardWatchEventKinds.ENTRY_MODIFY,
                    StandardWatchEventKinds.ENTRY_CREATE,
                    StandardWatchEventKinds.ENTRY_DELETE);
            log.info("Watching config directory: {}", dir);

            while (running.get()) {
                WatchKey key = watcher.poll(2, TimeUnit.SECONDS);
                if (key == null) {
                    continue;
                }
                for (WatchEvent<?> event : key.pollEvents()) {
                    Object context = event.context();
                    if (context instanceof Path changed && changed.toString().endsWith(".yaml")) {
                        log.info("Config changed: {}", changed);
                        agentFactory.clearCache();
                    }
                }
                if (!key.reset()) {
                    break;
                }
            }
        } catch (IOException e) {
            log.warn("Config watcher IO error: {}", e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
