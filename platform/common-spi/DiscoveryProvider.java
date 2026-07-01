package common.spi;

import java.util.Map;

public class DiscoveryProvider {
    public static ServiceDiscovery load() {
        String mode = System.getenv("DISCOVERY_MODE");
        if ("nacos".equalsIgnoreCase(mode)) {
            return new NacosDiscovery();
        } else {
            // fallback to static config
            return new StaticDiscovery(Map.of(
                "memory-server", "http://localhost:8081",
                "filesearch-server", "http://localhost:8082"
            ));
        }
    }
}
