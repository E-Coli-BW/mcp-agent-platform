package com.example.auth.service;

import com.example.auth.config.AuthTokenProperties;
import com.example.auth.model.AuthUser;
import com.example.auth.model.OutboxEvent;
import com.example.auth.model.Role;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.repository.OutboxEventRepository;
import com.example.auth.repository.RoleRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Jwts;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

@Service
public class UserService {

    private static final Logger log = LoggerFactory.getLogger(UserService.class);

    private final AuthUserRepository userRepo;
    private final RoleRepository roleRepo;
    private final OutboxEventRepository outboxRepo;
    private final RsaKeyManager keyManager;
    private final RefreshTokenService refreshTokenService;
    private final RedisRateLimiter redisRateLimiter;
    private final TokenBlacklistService tokenBlacklistService;
    private final AuthTokenProperties tokenProperties;
    private final BCryptPasswordEncoder passwordEncoder = new BCryptPasswordEncoder();

    // In-memory rate limiting (fallback when Redis is down)
    private final ConcurrentHashMap<String, LoginAttempt> loginAttempts = new ConcurrentHashMap<>();

    public UserService(
            AuthUserRepository userRepo,
            RoleRepository roleRepo,
            OutboxEventRepository outboxRepo,
            RsaKeyManager keyManager,
            RefreshTokenService refreshTokenService,
            RedisRateLimiter redisRateLimiter,
            TokenBlacklistService tokenBlacklistService,
            AuthTokenProperties tokenProperties) {
        this.userRepo = userRepo;
        this.roleRepo = roleRepo;
        this.outboxRepo = outboxRepo;
        this.keyManager = keyManager;
        this.refreshTokenService = refreshTokenService;
        this.redisRateLimiter = redisRateLimiter;
        this.tokenBlacklistService = tokenBlacklistService;
        this.tokenProperties = tokenProperties;
    }

    /**
     * Register a new user.
     */
    @Transactional
    public SignupResponse signup(String username, String password, String email, String tenantId) {
        // Validation
        if (username == null || username.isBlank()) {
            throw new UserException("Username is required");
        }
        if (password == null || password.length() < 8) {
            throw new UserException("Password must be at least 8 characters");
        }
        if (tenantId == null || tenantId.isBlank()) {
            throw new UserException("tenant_id is required");
        }
        if (userRepo.existsByUsername(username)) {
            throw new DuplicateUserException("Username already exists");
        }

        String hash = passwordEncoder.encode(password);
        AuthUser user = new AuthUser(username, hash, email, tenantId);
        user = userRepo.save(user);

        Role userRole = roleRepo.findByName("USER")
                .orElseThrow(() -> new RuntimeException("Default USER role not found"));
        user.getRoles().add(userRole);
        user = userRepo.save(user);

        String payload;
        try {
            var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            payload = mapper.writeValueAsString(java.util.Map.of(
                    "type", "USER_REGISTERED",
                    "userId", user.getId(),
                    "username", username,
                    "email", email != null ? email : "",
                    "tenantId", tenantId,
                    "timestamp", Instant.now().toString()
            ));
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new RuntimeException("Failed to serialize outbox payload", e);
        }
        OutboxEvent event = new OutboxEvent(
                UUID.randomUUID().toString(),
                "user.events",
                tenantId,
                payload
        );
        outboxRepo.save(event);

        log.info("User registered: username={}, tenant={}", username, tenantId);
        return new SignupResponse(user.getId(), username, tenantId);
    }

    /**
     * Authenticate user and issue a JWT.
     *
     * <p>Failure modes (each maps to a distinct exception so the
     * {@link com.example.auth.api.GlobalExceptionHandler} can return
     * a useful error code to the client):</p>
     * <ul>
     *   <li>{@link RateLimitedException} — Redis rate limiter blocked request</li>
     *   <li>{@link AccountLockedException} — in-memory fail counter exceeded</li>
     *   <li>{@link AccountDisabledException} — admin disabled the user</li>
     *   <li>{@link InvalidCredentialsException} — user not found OR wrong password
     *       (intentionally indistinguishable to prevent username enumeration)</li>
     * </ul>
     *
     * <p>Every failure path logs the attempted username at WARN. The client
     * still receives a generic 401 message for credential failures — the log
     * is for the operator only.</p>
     */
    public LoginResponse login(String username, String password) {
        log.debug("Login attempt: username={}", username);

        // Check rate limit — Redis first, fall back to in-memory
        String rateLimitKey = "rate:login:user:" + username;
        if (!redisRateLimiter.tryAcquire(rateLimitKey)) {
            log.warn("Login blocked by rate limiter: username={}", username);
            throw new RateLimitedException("Too many login attempts. Try again later.");
        }
        if (isLockedOut(username)) {
            log.warn("Login blocked by in-memory lockout: username={}", username);
            throw new AccountLockedException(
                    "Account temporarily locked due to too many failed attempts. Try again in 5 minutes.");
        }

        AuthUser user = userRepo.findByUsername(username)
                .orElseThrow(() -> {
                    recordFailedAttempt(username);
                    log.warn("Login failed (no such user): username={}", username);
                    return new InvalidCredentialsException("Invalid username or password");
                });

        if (!user.isEnabled()) {
            log.warn("Login blocked (account disabled): username={}", username);
            throw new AccountDisabledException("Account is disabled");
        }

        if (!passwordEncoder.matches(password, user.getPasswordHash())) {
            recordFailedAttempt(username);
            log.warn("Login failed (wrong password): username={}", username);
            throw new InvalidCredentialsException("Invalid username or password");
        }

        // Success — reset attempts (both Redis and in-memory)
        loginAttempts.remove(username);
        redisRateLimiter.reset(rateLimitKey);

        Set<String> roleNames = user.getRoles().stream()
                .map(role -> "ROLE_" + role.getName())
                .collect(Collectors.toSet());
        Set<String> permissions = user.getRoles().stream()
                .flatMap(role -> role.getPermissions().stream())
                .collect(Collectors.toSet());

        // Issue JWT with user claims
        Instant now = Instant.now();
        String token = Jwts.builder()
                .header().keyId(keyManager.getKeyId()).and()
                .issuer("mcp-auth-service")
                .subject(username)
                .audience().add("agent-server").and()
                .claim("tenant_id", user.getTenantId())
                .claim("sub_type", "USER")
                .claim("roles", roleNames)
                .claim("permissions", permissions)
                .id(UUID.randomUUID().toString())
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(tokenProperties.getAccessTtlSeconds(), ChronoUnit.SECONDS)))
                .signWith(keyManager.getPrivateKey())
                .compact();

        String refreshToken = refreshTokenService.createRefreshToken(user.getId(), null);

        log.info("User login: username={}, tenant={}", username, user.getTenantId());
        return new LoginResponse(token, "Bearer", tokenProperties.getAccessTtlSeconds(), user.getTenantId(), refreshToken);
    }

    // ── Rate Limiting ────────────────────────────────────────

    private boolean isLockedOut(String username) {
        LoginAttempt attempt = loginAttempts.get(username);
        if (attempt == null) return false;
        if (attempt.failCount.get() >= tokenProperties.getMaxFailedAttempts()) {
            if (System.currentTimeMillis() < attempt.lockoutUntil) {
                return true;
            }
            // Lockout expired — reset
            loginAttempts.remove(username);
            return false;
        }
        return false;
    }

    private void recordFailedAttempt(String username) {
        loginAttempts.compute(username, (k, existing) -> {
            if (existing == null) {
                existing = new LoginAttempt();
            }
            int count = existing.failCount.incrementAndGet();
            if (count >= tokenProperties.getMaxFailedAttempts()) {
                existing.lockoutUntil = System.currentTimeMillis() + tokenProperties.getLockoutDurationMs();
                log.warn("Account locked: username={}, attempts={}", username, count);
            }
            return existing;
        });
    }

    // ── Logout ────────────────────────────────────────────────

    /**
     * Logout — blacklist the access token and revoke all refresh tokens.
     * Moved from AuthController (A4: controller should not access repository directly).
     */
    public void logout(String accessToken) {
        try {
            var claims = Jwts.parser()
                    .verifyWith(keyManager.getPublicKey())
                    .build()
                    .parseSignedClaims(accessToken)
                    .getPayload();

            String username = claims.getSubject();
            String jti = claims.getId();

            if (jti != null) {
                tokenBlacklistService.blacklist(jti, claims.getExpiration().toInstant());
            }

            userRepo.findByUsername(username)
                    .ifPresent(user -> refreshTokenService.revokeAllForUser(user.getId()));

            log.info("User logged out: username={}", username);
        } catch (Exception e) {
            log.warn("Logout failed: {}", e.getMessage());
            throw new InvalidCredentialsException("Invalid or expired token");
        }
    }

    // ── Inner classes ────────────────────────────────────────

    private static class LoginAttempt {
        final AtomicInteger failCount = new AtomicInteger(0);
        volatile long lockoutUntil = 0;
    }

    // ── DTOs ─────────────────────────────────────────────────

    public record SignupResponse(Long userId, String username, String tenantId) {}
    public record LoginResponse(String accessToken, String tokenType, long expiresIn, String tenantId, String refreshToken) {}

    // ── Exceptions ───────────────────────────────────────────

    public static class UserException extends RuntimeException {
        public UserException(String message) { super(message); }
    }

    public static class DuplicateUserException extends UserException {
        public DuplicateUserException(String message) { super(message); }
    }

    public static class InvalidCredentialsException extends UserException {
        public InvalidCredentialsException(String message) { super(message); }
    }

    /** Login rejected by the rate limiter (Redis sliding window). */
    public static class RateLimitedException extends UserException {
        public RateLimitedException(String message) { super(message); }
    }

    /** Account locked out by the in-memory fail counter (Redis fallback). */
    public static class AccountLockedException extends UserException {
        public AccountLockedException(String message) { super(message); }
    }

    /** Account disabled by an administrator. */
    public static class AccountDisabledException extends UserException {
        public AccountDisabledException(String message) { super(message); }
    }
}
