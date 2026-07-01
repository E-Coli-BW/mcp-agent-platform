package common.spi;

public interface ServiceDiscovery {
    String resolve(String serviceName);
}
