package common.spi;

public class NacosDiscovery implements ServiceDiscovery {
    @Override
    public String resolve(String serviceName) {
        // TODO: Integrate with Nacos SDK
        throw new UnsupportedOperationException("Nacos integration not implemented yet");
    }
}
