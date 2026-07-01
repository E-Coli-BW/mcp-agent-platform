package com.example.auth.service;

import com.example.auth.model.AuthUser;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import static org.junit.jupiter.api.Assertions.*;

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-user-service-test;DB_CLOSE_DELAY=-1")
class UserServiceTest {

    @Autowired
    private UserService userService;

    @Autowired
    private AuthUserRepository userRepo;

    @Autowired
    private RsaKeyManager keyManager;

    /**
     * Mock the Redis-backed rate limiter so this test never touches a real Redis
     * instance. Without this, a developer running `mvn test` on a workstation
     * that happens to have Redis on localhost:6379 will share the
     * `rate:login:user:grace` sliding window with previous runs and eventually
     * trip {@code RateLimitedException} instead of the
     * {@code InvalidCredentialsException} / {@code AccountLockedException}
     * this test is exercising.
     *
     * <p>The test verifies the IN-MEMORY lockout path (5 failures →
     * AccountLockedException), which is independent of the distributed
     * Redis rate limiter. Mocking the limiter to always allow keeps the
     * two concerns separated and makes the test hermetic.</p>
     */
    @MockitoBean
    private RedisRateLimiter redisRateLimiter;

    @BeforeEach
    void setup() {
        userRepo.deleteAll();
        // Always allow — we are testing the in-memory lockout, not Redis.
        Mockito.when(redisRateLimiter.tryAcquire(Mockito.anyString())).thenReturn(true);
    }

    @Test
    void signup_success() {
        var result = userService.signup("alice", "password123", "alice@test.com", "tenant-1");
        assertNotNull(result.userId());
        assertEquals("alice", result.username());
        assertEquals("tenant-1", result.tenantId());
        assertTrue(userRepo.existsByUsername("alice"));
    }

    @Test
    void signup_duplicateUsername_throws() {
        userService.signup("bob", "password123", null, "tenant-1");
        assertThrows(UserService.DuplicateUserException.class, () ->
            userService.signup("bob", "other-pass123", null, "tenant-1")
        );
    }

    @Test
    void signup_shortPassword_throws() {
        assertThrows(UserService.UserException.class, () ->
            userService.signup("charlie", "short", null, "tenant-1")
        );
    }

    @Test
    void login_success() {
        userService.signup("dave", "password123", null, "tenant-2");
        var result = userService.login("dave", "password123");
        assertNotNull(result.accessToken());
        assertEquals("Bearer", result.tokenType());
        assertEquals("tenant-2", result.tenantId());
        assertEquals(3600, result.expiresIn());
    }

    @Test
    void login_wrongPassword_throws() {
        userService.signup("eve", "password123", null, "tenant-1");
        assertThrows(UserService.InvalidCredentialsException.class, () ->
            userService.login("eve", "wrong-password")
        );
    }

    @Test
    void login_nonexistentUser_throws() {
        assertThrows(UserService.InvalidCredentialsException.class, () ->
            userService.login("nobody", "password123")
        );
    }

    @Test
    void login_jwtHasCorrectClaims() {
        userService.signup("frank", "password123", null, "tenant-3");
        var result = userService.login("frank", "password123");

        // Verify JWT claims using the public key
        Claims claims = Jwts.parser()
                .verifyWith(keyManager.getPublicKey())
                .build()
                .parseSignedClaims(result.accessToken())
                .getPayload();

        assertEquals("frank", claims.getSubject());
        assertEquals("tenant-3", claims.get("tenant_id", String.class));
        assertEquals("USER", claims.get("sub_type", String.class));
        assertTrue(claims.getAudience().contains("agent-server"));
        assertEquals("mcp-auth-service", claims.getIssuer());
    }

    @Test
    void login_rateLimiting_locksAfter5Failures() {
        userService.signup("grace", "password123", null, "tenant-1");

        // 5 failed attempts
        for (int i = 0; i < 5; i++) {
            assertThrows(UserService.InvalidCredentialsException.class, () ->
                userService.login("grace", "wrong-pass")
            );
        }

        // 6th attempt should be locked even with correct password.
        // Specifically AccountLockedException — NOT generic UserException,
        // NOT InvalidCredentialsException. Distinct type so the handler
        // can return 423 + a useful error code to the client.
        var ex = assertThrows(UserService.AccountLockedException.class, () ->
            userService.login("grace", "password123")
        );
        assertTrue(ex.getMessage().contains("locked"));
    }

    @Test
    void login_disabledUser_throwsAccountDisabled() {
        userService.signup("henry", "password123", null, "tenant-1");
        // Disable user directly
        AuthUser user = userRepo.findByUsername("henry").orElseThrow();
        user.setEnabled(false);
        userRepo.save(user);

        // Specifically AccountDisabledException — the handler maps it to 403
        // with `account_disabled` so the frontend can tell the user to contact
        // an admin instead of showing the generic "Invalid username or password".
        assertThrows(UserService.AccountDisabledException.class, () ->
            userService.login("henry", "password123")
        );
    }

    /**
     * Pin the precise exception type hierarchy so the {@link com.example.auth.api.GlobalExceptionHandler}
     * keeps mapping each failure mode to the right HTTP status. Without this,
     * a refactor that re-merges two sibling exceptions into UserException would
     * silently collapse two distinct UI errors into the catch-all "Operation
     * not permitted" message — which is exactly the bug that motivated this
     * split. See `skill-auth-error-modes-should-be-distinguishable` in memory.
     */
    @Test
    void exceptionHierarchy_pins_distinctTypes() {
        // All four extend UserException (so the catch-all handler still works
        // as a safety net) but each one is independently throwable so the
        // dedicated handlers can fire first.
        assertTrue(UserService.UserException.class.isAssignableFrom(
                UserService.InvalidCredentialsException.class));
        assertTrue(UserService.UserException.class.isAssignableFrom(
                UserService.RateLimitedException.class));
        assertTrue(UserService.UserException.class.isAssignableFrom(
                UserService.AccountLockedException.class));
        assertTrue(UserService.UserException.class.isAssignableFrom(
                UserService.AccountDisabledException.class));

        // The four siblings must NOT be assignable from each other — if
        // someone collapses two of them by mistake, this test catches it.
        assertNotSame(UserService.InvalidCredentialsException.class,
                UserService.AccountLockedException.class);
        assertNotSame(UserService.InvalidCredentialsException.class,
                UserService.AccountDisabledException.class);
        assertNotSame(UserService.InvalidCredentialsException.class,
                UserService.RateLimitedException.class);
        assertNotSame(UserService.AccountLockedException.class,
                UserService.AccountDisabledException.class);
    }
}
