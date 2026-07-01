package com.example.auth.service;

import com.example.auth.config.AuthTokenProperties;
import com.example.auth.model.AuthUser;
import com.example.auth.model.RefreshToken;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.repository.RefreshTokenRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Jwts;
import jakarta.transaction.Transactional;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

@Service
public class RefreshTokenService {

    private final RefreshTokenRepository refreshTokenRepo;
    private final AuthUserRepository userRepo;
    private final RsaKeyManager keyManager;
    private final AuthTokenProperties tokenProperties;

    public RefreshTokenService(
            RefreshTokenRepository refreshTokenRepo,
            AuthUserRepository userRepo,
            RsaKeyManager keyManager,
            AuthTokenProperties tokenProperties) {
        this.refreshTokenRepo = refreshTokenRepo;
        this.userRepo = userRepo;
        this.keyManager = keyManager;
        this.tokenProperties = tokenProperties;
    }

    /**
     * Create a new refresh token for a user.
     * Generates a random UUID token, stores SHA-256 hash in DB.
     *
     * @return the raw refresh token (only returned once, never stored in plaintext)
     */
    public String createRefreshToken(Long userId, String deviceInfo) {
        String rawToken = UUID.randomUUID().toString();
        String tokenHash = sha256(rawToken);

        RefreshToken entity = new RefreshToken(
                UUID.randomUUID().toString(),
                userId,
                tokenHash,
                Instant.now().plus(tokenProperties.getRefreshTtlDays(), ChronoUnit.DAYS),
                deviceInfo
        );
        refreshTokenRepo.save(entity);
        return rawToken;
    }

    /**
     * Validate a refresh token and issue a new access token + refresh token pair.
     * Implements token rotation: old refresh token is revoked.
     */
    public TokenPair refreshAccessToken(String rawRefreshToken) {
        String tokenHash = sha256(rawRefreshToken);
        RefreshToken stored = refreshTokenRepo.findByTokenHashAndRevokedFalse(tokenHash)
                .orElseThrow(() -> new InvalidRefreshTokenException("Invalid or revoked refresh token"));

        if (stored.getExpiresAt().isBefore(Instant.now())) {
            stored.setRevoked(true);
            refreshTokenRepo.save(stored);
            throw new InvalidRefreshTokenException("Refresh token expired");
        }

        stored.setRevoked(true);
        refreshTokenRepo.save(stored);

        AuthUser user = userRepo.findById(stored.getUserId())
                .orElseThrow(() -> new InvalidRefreshTokenException("User not found"));

        if (!user.isEnabled()) {
            throw new InvalidRefreshTokenException("User account is disabled");
        }

        String accessToken = buildAccessToken(user);
        String newRefreshToken = createRefreshToken(user.getId(), stored.getDeviceInfo());

        return new TokenPair(accessToken, newRefreshToken, tokenProperties.getAccessTtlSeconds());
    }

    /**
     * Revoke all refresh tokens for a user (logout everywhere).
     */
    @Transactional
    public void revokeAllForUser(Long userId) {
        refreshTokenRepo.revokeAllByUserId(userId);
    }

    private String buildAccessToken(AuthUser user) {
        Set<String> roleNames = user.getRoles().stream()
                .map(role -> "ROLE_" + role.getName())
                .collect(Collectors.toSet());
        Set<String> permissions = user.getRoles().stream()
                .flatMap(role -> role.getPermissions().stream())
                .collect(Collectors.toSet());

        Instant now = Instant.now();
        return Jwts.builder()
                .header().keyId(keyManager.getKeyId()).and()
                .issuer("mcp-auth-service")
                .subject(user.getUsername())
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
    }

    private String sha256(String input) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(input.getBytes(StandardCharsets.UTF_8));
            return bytesToHex(hash);
        } catch (Exception e) {
            throw new RuntimeException("SHA-256 not available", e);
        }
    }

    private String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder();
        for (byte b : bytes) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    public record TokenPair(String accessToken, String refreshToken, long expiresIn) {}

    public static class InvalidRefreshTokenException extends RuntimeException {
        public InvalidRefreshTokenException(String message) {
            super(message);
        }
    }
}
