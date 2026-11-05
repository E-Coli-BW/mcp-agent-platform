package com.example.memoryserver;

import com.example.memorystore.MemoryStoreService;
import com.example.memorystore.spi.MemoryStorageBackend;
import com.example.memorystore.spi.ServiceDiscovery;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(properties = {
    "spring.flyway.enabled=false",
        "memorystore.backend=file",
        "memorystore.discovery=static",
        "memorystore.file.base-dir=${java.io.tmpdir}/memory-store-composition-test"
})
class MemoryStoreProductionCompositionTest {

    @Autowired(required = false)
    private MemoryStoreService memoryStoreService;

    @Autowired(required = false)
    private MemoryStorageBackend memoryStorageBackend;

    @Autowired(required = false)
    private ServiceDiscovery serviceDiscovery;

    @Test
    void productionApplicationComposesTheSupportedMemoryStoreSpi() {
        assertThat(memoryStoreService).isNotNull();
        assertThat(memoryStorageBackend).isNotNull();
        assertThat(serviceDiscovery).isNotNull();
    }
}
