package com.example.auth.config;

import com.example.auth.model.Role;
import com.example.auth.repository.RoleRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.stereotype.Component;

import java.util.Set;

@Component
public class DefaultRoleInitializer implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(DefaultRoleInitializer.class);

    private final RoleRepository roleRepository;

    public DefaultRoleInitializer(RoleRepository roleRepository) {
        this.roleRepository = roleRepository;
    }

    @Override
    public void run(String... args) {
        seedRole("SUPER_ADMIN", "Full system access", Set.of("*"));
        seedRole("TENANT_ADMIN", "Tenant-level administration",
                Set.of("USER_MANAGE", "MEMORY_READ", "MEMORY_WRITE", "CHAT", "SETTINGS", "AUDIT_READ"));
        seedRole("USER", "Standard user access",
                Set.of("MEMORY_READ", "MEMORY_WRITE", "CHAT", "SETTINGS_SELF"));
        seedRole("VIEWER", "Read-only access", Set.of("MEMORY_READ", "CHAT_READ"));

        log.info("✅ Default roles registered");
    }

    private void seedRole(String name, String description, Set<String> permissions) {
        if (roleRepository.findByName(name).isPresent()) {
            return;
        }

        roleRepository.save(new Role(name, description, permissions));
        log.info("  Registered role: {}", name);
    }
}
