package com.example.auth.service;

import com.example.auth.model.AuthUser;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-rbac-test;DB_CLOSE_DELAY=-1")
class RbacTest {

    @Autowired
    private UserService userService;

    @Autowired
    private AuthUserRepository userRepo;

    @Autowired
    private RsaKeyManager keyManager;

    @BeforeEach
    void setup() {
        userRepo.deleteAll();
    }

    @Test
    void signup_assignsUserRole() {
        userService.signup("ivy", "password123", "ivy@test.com", "tenant-1");

        AuthUser user = userRepo.findByUsername("ivy").orElseThrow();
        assertTrue(user.getRoles().stream().anyMatch(role -> "USER".equals(role.getName())));
    }

    @Test
    void login_jwtContainsRolesAndPermissionsClaims() {
        userService.signup("jane", "password123", null, "tenant-2");
        var result = userService.login("jane", "password123");

        Claims claims = parseClaims(result.accessToken());
        Set<String> roles = claimSet(claims, "roles");
        Set<String> permissions = claimSet(claims, "permissions");

        assertTrue(roles.contains("ROLE_USER"));
        assertFalse(permissions.isEmpty());
    }

    @Test
    void login_permissionsIncludeDefaultUserPermissions() {
        userService.signup("kate", "password123", null, "tenant-3");
        var result = userService.login("kate", "password123");

        Claims claims = parseClaims(result.accessToken());
        Set<String> permissions = claimSet(claims, "permissions");

        assertTrue(permissions.containsAll(Set.of("MEMORY_READ", "MEMORY_WRITE", "CHAT", "SETTINGS_SELF")));
    }

    private Claims parseClaims(String token) {
        return Jwts.parser()
                .verifyWith(keyManager.getPublicKey())
                .build()
                .parseSignedClaims(token)
                .getPayload();
    }

    private Set<String> claimSet(Claims claims, String claimName) {
        List<?> values = claims.get(claimName, List.class);
        assertNotNull(values);
        return values.stream()
                .map(Object::toString)
                .collect(Collectors.toSet());
    }
}
