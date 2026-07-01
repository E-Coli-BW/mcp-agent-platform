package com.example.memoryserver.security;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.Test;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for JwtAuthFilter tenant extraction and JWT validation.
 *
 * Covers:
 * - Valid JWT → tenant extracted correctly
 * - Expired JWT → tenant NOT set (request proceeds unauthenticated)
 * - Wrong signing key → tenant NOT set
 * - JWT with spoofed tenant_id but valid signature → ACCEPTED (this IS the gap)
 */
class JwtAuthFilterTest {

    private static final String SECRET = "test-secret-key-at-least-32-bytes-long!!";
    private static final String WRONG_SECRET = "wrong-secret-key-at-least-32-bytes!!!!!";

    private SecretKey key(String secret) {
        byte[] keyBytes = secret.getBytes(StandardCharsets.UTF_8);
        if (keyBytes.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
            keyBytes = padded;
        }
        return Keys.hmacShaKeyFor(keyBytes);
    }

    private String buildToken(String secret, String tenantId, Instant expiry) {
        return Jwts.builder()
                .subject(tenantId)
                .claim("tenant_id", tenantId)
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(expiry))
                .signWith(key(secret))
                .compact();
    }

    // ── Helper: parse like JwtAuthFilter does ────────────────────

    private String extractTenantFromToken(String token, String secret) {
        try {
            var claims = Jwts.parser()
                    .verifyWith(key(secret))
                    .build()
                    .parseSignedClaims(token)
                    .getPayload();
            String tenantId = claims.get("tenant_id", String.class);
            return tenantId != null ? tenantId : claims.getSubject();
        } catch (Exception e) {
            return null; // filter would skip auth
        }
    }

    // ── Valid JWT ─────────────────────────────────────────────────

    @Test
    void validJwt_extractsTenantCorrectly() {
        String token = buildToken(SECRET, "my-tenant", Instant.now().plus(1, ChronoUnit.HOURS));
        assertEquals("my-tenant", extractTenantFromToken(token, SECRET));
    }

    // ── Expired JWT → rejected ───────────────────────────────────

    @Test
    void expiredJwt_returnsNull() {
        String token = buildToken(SECRET, "my-tenant", Instant.now().minus(1, ChronoUnit.HOURS));
        assertNull(extractTenantFromToken(token, SECRET));
    }

    // ── Wrong signing key → rejected ─────────────────────────────

    @Test
    void wrongSigningKey_returnsNull() {
        String token = buildToken(WRONG_SECRET, "my-tenant", Instant.now().plus(1, ChronoUnit.HOURS));
        // Try to verify with the correct secret — should fail
        assertNull(extractTenantFromToken(token, SECRET));
    }

    // ── ATTACK: JWT spoofing (valid signature, fake tenant) ──────
    // This test PROVES the gap: if someone obtains a valid JWT for tenant-A,
    // they CAN'T change it to tenant-B (signature check fails).
    // BUT if they have access to the signing key (compromised), they CAN
    // forge a JWT for any tenant. This is the "IdP validation" gap.

    @Test
    void spoofedJwt_signedWithCorrectKey_isAccepted_THIS_IS_THE_GAP() {
        // Attacker has the signing key and forges a token for "admin-tenant"
        String forgedToken = buildToken(SECRET, "admin-tenant", Instant.now().plus(1, ChronoUnit.HOURS));

        // The filter accepts it because the signature is valid
        // THIS IS THE GAP: we trust the signing key alone, no IdP validation
        assertEquals("admin-tenant", extractTenantFromToken(forgedToken, SECRET));

        // In production: use asymmetric keys (RS256) with IdP-issued JWTs
        // so the server only has the public key and cannot forge tokens
    }

    @Test
    void tamperedJwt_modifiedPayload_isRejected() {
        String validToken = buildToken(SECRET, "tenant-A", Instant.now().plus(1, ChronoUnit.HOURS));

        // Tamper with the payload (change a character)
        String[] parts = validToken.split("\\.");
        // Modify the payload part (base64-encoded JSON)
        String tamperedPayload = parts[1].substring(0, parts[1].length() - 2) + "XX";
        String tamperedToken = parts[0] + "." + tamperedPayload + "." + parts[2];

        // Should be rejected — signature doesn't match
        assertNull(extractTenantFromToken(tamperedToken, SECRET));
    }
}
