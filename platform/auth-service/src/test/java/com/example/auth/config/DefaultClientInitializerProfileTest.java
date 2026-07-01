package com.example.auth.config;

import com.example.auth.repository.AuthClientRepository;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.test.context.ActiveProfiles;

import static org.junit.jupiter.api.Assertions.assertTrue;

abstract class BaseDefaultClientInitializerProfileTest {

    protected final AuthClientRepository clientRepository;

    protected BaseDefaultClientInitializerProfileTest(AuthClientRepository clientRepository) {
        this.clientRepository = clientRepository;
    }

    protected void assertSeededClients() {
        assertTrue(clientRepository.findByClientId("agent-server").isPresent());
        assertTrue(clientRepository.findByClientId("web-frontend").isPresent());
        assertTrue(clientRepository.findByClientId("admin-cli").isPresent());
    }
}

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:default-client-initializer-dev;DB_CLOSE_DELAY=-1")
@ActiveProfiles("dev")
class DefaultClientInitializerProfileTest extends BaseDefaultClientInitializerProfileTest {

    @Autowired
    DefaultClientInitializerProfileTest(AuthClientRepository clientRepository) {
        super(clientRepository);
    }

    @Test
    void should_seedClients_when_devProfile() {
        assertSeededClients();
    }
}

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:default-client-initializer-prod;DB_CLOSE_DELAY=-1")
@ActiveProfiles("prod")
class DefaultClientInitializerProdProfileTest extends BaseDefaultClientInitializerProfileTest {

    @Autowired
    DefaultClientInitializerProdProfileTest(AuthClientRepository clientRepository) {
        super(clientRepository);
    }

    @Test
    void should_skipSeeding_when_prodProfile() {
        assertTrue(clientRepository.findByClientId("agent-server").isEmpty());
        assertTrue(clientRepository.findByClientId("web-frontend").isEmpty());
        assertTrue(clientRepository.findByClientId("admin-cli").isEmpty());
    }
}

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:default-client-initializer-test;DB_CLOSE_DELAY=-1")
@ActiveProfiles("test")
@Import(DefaultClientInitializerTestSupport.class)
class DefaultClientInitializerTestProfileTest extends BaseDefaultClientInitializerProfileTest {

    @Autowired
    DefaultClientInitializerTestProfileTest(AuthClientRepository clientRepository) {
        super(clientRepository);
    }

    @Test
    void should_seedClients_when_testProfile() {
        assertSeededClients();
    }
}

@TestConfiguration(proxyBeanMethods = false)
class DefaultClientInitializerTestSupport {

    @Bean
    StringRedisTemplate stringRedisTemplate() {
        return Mockito.mock(StringRedisTemplate.class);
    }
}
