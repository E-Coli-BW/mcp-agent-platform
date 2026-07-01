package common.spi;

import java.util.Map;

public class StaticDiscovery implements ServiceDiscovery {
    private final Map<String, String> staticMap;
    public StaticDiscovery(Map<String, String> staticMap) {
        this.staticMap = staticMap;
    }
    @Override
    public String resolve(String serviceName) {
        return staticMap.get(serviceName);
    }
}
