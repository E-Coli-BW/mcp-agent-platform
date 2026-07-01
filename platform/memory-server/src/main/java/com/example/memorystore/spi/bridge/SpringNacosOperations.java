package com.example.memorystore.spi.bridge;

import com.alibaba.nacos.api.naming.NamingService;
import com.alibaba.nacos.api.naming.pojo.Instance;
import com.example.memorystore.spi.impl.NacosServiceDiscovery;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.stream.Collectors;

/**
 * Bridges Nacos NamingService SDK to our NacosOperations SPI.
 * Use this in production Spring context to wire NacosServiceDiscovery.
 */
public class SpringNacosOperations implements NacosServiceDiscovery.NacosOperations {

    private static final Logger log = LoggerFactory.getLogger(SpringNacosOperations.class);

    private final NamingService namingService;

    public SpringNacosOperations(NamingService namingService) {
        this.namingService = namingService;
    }

    @Override
    public List<String> getInstances(String serviceName) {
        try {
            List<Instance> instances = namingService.selectInstances(serviceName, true);
            return instances.stream()
                    .map(i -> i.getIp() + ":" + i.getPort())
                    .collect(Collectors.toList());
        } catch (Exception e) {
            log.warn("Nacos discovery failed for service '{}': {}", serviceName, e.getMessage());
            return List.of();
        }
    }
}
