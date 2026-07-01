package com.example.codeexec.sandbox;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.NoSuchBeanDefinitionException;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ProcessSandboxProfileTest {

    private final ApplicationContextRunner contextRunner = new ApplicationContextRunner()
            .withUserConfiguration(TestConfiguration.class);

    @Test
    void should_notBeRegistered_when_defaultProfile() {
        contextRunner.run(context -> assertThrows(NoSuchBeanDefinitionException.class,
                () -> context.getBean(ProcessSandbox.class)));
    }

    @Test
    void should_beRegistered_when_devProfileAndProcessMode() {
        contextRunner
                .withInitializer(context -> context.getEnvironment().setActiveProfiles("dev"))
                .withPropertyValues("codeexec.sandbox.mode=process")
                .run(context -> assertNotNull(context.getBean(ProcessSandbox.class)));
    }

    @Configuration(proxyBeanMethods = false)
    @ComponentScan(
            basePackageClasses = ProcessSandbox.class,
            excludeFilters = @ComponentScan.Filter(type = FilterType.ASSIGNABLE_TYPE, classes = DockerSandbox.class))
    static class TestConfiguration {
    }
}

class ProcessSandboxSanitizationTest {

    @Test
    void should_sanitizeTenantId_inWorkDir() {
        assertEquals("etc", ProcessSandbox.sanitizeTenantId("../../etc"));
        assertEquals("a_b_c", ProcessSandbox.sanitizeTenantId("a/b\\c"));
        assertThrows(SecurityException.class, () -> ProcessSandbox.sanitizeTenantId(""));
        assertThrows(SecurityException.class, () -> ProcessSandbox.sanitizeTenantId(null));
        assertThrows(SecurityException.class, () -> ProcessSandbox.sanitizeTenantId(".."));
        assertEquals("normal-tenant_1", ProcessSandbox.sanitizeTenantId("normal-tenant_1"));
    }
}
