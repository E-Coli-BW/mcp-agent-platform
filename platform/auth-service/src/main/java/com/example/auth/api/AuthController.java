package com.example.auth.api;

import com.example.auth.security.RsaKeyManager;
import com.example.auth.service.RefreshTokenService;
import com.example.auth.service.TokenService;
import com.example.auth.service.TokenService.TokenResponse;
import com.example.auth.service.UserService;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

import static com.example.auth.api.AuthErrorCode.*;

/**
 * Auth Service REST API — thin controller, delegates to services.
 *
 * <p>All exception handling is in {@link GlobalExceptionHandler}.</p>
 * <p>All request bodies use typed DTOs with validation and safe toString().</p>
 */
@RestController
@RequestMapping("/auth")
public class AuthController {

    private final TokenService tokenService;
    private final UserService userService;
    private final RsaKeyManager keyManager;
    private final RefreshTokenService refreshTokenService;

    public AuthController(TokenService tokenService, UserService userService,
                          RsaKeyManager keyManager, RefreshTokenService refreshTokenService) {
        this.tokenService = tokenService;
        this.userService = userService;
        this.keyManager = keyManager;
        this.refreshTokenService = refreshTokenService;
    }

    /**
     * Token endpoint — OAuth2 client_credentials or password grant.
     */
    @PostMapping("/token")
    public ResponseEntity<Map<String, Object>> token(@Valid @RequestBody TokenRequest request) {
        String grantType = request.grantType();
        String audience = request.audience() != null ? request.audience() : DEFAULT_AUDIENCE;

        return switch (grantType) {
            case "client_credentials" -> {
                requireNonBlank(request.clientId(), "client_id");
                requireNonBlank(request.clientSecret(), "client_secret");
                TokenResponse resp = tokenService.issueToken(
                        request.clientId(), request.clientSecret(), audience, request.tenantId());
                yield ResponseEntity.ok(Map.of(
                        "access_token", resp.accessToken(),
                        "token_type", resp.tokenType(),
                        "expires_in", resp.expiresIn()));
            }
            case "password" -> {
                requireNonBlank(request.username(), "username");
                requireNonBlank(request.password(), "password");
                var resp = userService.login(request.username(), request.password());
                yield ResponseEntity.ok(Map.of(
                        "access_token", resp.accessToken(),
                        "token_type", resp.tokenType(),
                        "expires_in", resp.expiresIn(),
                        "tenant_id", resp.tenantId(),
                        "refresh_token", resp.refreshToken()));
            }
            case null -> throw new IllegalArgumentException("grant_type is required");
            default -> throw new IllegalArgumentException("Unsupported grant_type: " + grantType
                    + ". Supported: client_credentials, password");
        };
    }

    @PostMapping("/login")
    public ResponseEntity<Map<String, Object>> login(@Valid @RequestBody LoginRequest request) {
        var resp = userService.login(request.username(), request.password());
        return ResponseEntity.ok(Map.of(
                "access_token", resp.accessToken(),
                "token_type", resp.tokenType(),
                "expires_in", resp.expiresIn(),
                "tenant_id", resp.tenantId(),
                "refresh_token", resp.refreshToken()));
    }

    @PostMapping("/refresh")
    public ResponseEntity<Map<String, Object>> refresh(@Valid @RequestBody RefreshRequest request) {
        var pair = refreshTokenService.refreshAccessToken(request.refreshToken());
        return ResponseEntity.ok(Map.of(
                "access_token", pair.accessToken(),
                "token_type", "Bearer",
                "expires_in", pair.expiresIn(),
                "refresh_token", pair.refreshToken()));
    }

    @PostMapping("/logout")
    public ResponseEntity<Map<String, String>> logout(
            @RequestHeader(value = "Authorization", required = false) String authHeader) {
        if (authHeader == null || !authHeader.startsWith("Bearer ")) {
            throw new IllegalArgumentException("Authorization Bearer token required");
        }
        userService.logout(authHeader.substring(7));
        return ResponseEntity.ok(Map.of("message", "Logged out successfully"));
    }

    @PostMapping("/signup")
    public ResponseEntity<Map<String, Object>> signup(@Valid @RequestBody SignupRequest request) {
        var resp = userService.signup(
                request.username(), request.password(), request.email(), request.tenantId());
        return ResponseEntity.ok(Map.of(
                "user_id", resp.userId(),
                "username", resp.username(),
                "tenant_id", resp.tenantId(),
                "message", "User created"));
    }

    @GetMapping("/jwks")
    public Map<String, Object> jwks() {
        return keyManager.getJwks();
    }

    @GetMapping("/check-blacklist")
    public Map<String, Object> checkBlacklist(@RequestParam("jti") String jti) {
        return Map.of("jti", jti, "blacklisted", tokenService.isBlacklisted(jti));
    }

    /**
     * Client registration — requires valid JWT with ADMIN role.
     * Security (S2): this endpoint was previously unprotected.
     */
    @PostMapping("/register")
    public ResponseEntity<Map<String, Object>> register(
            @Valid @RequestBody RegisterClientRequest request,
            @RequestHeader(value = "Authorization", required = false) String authHeader) {
        // Require admin auth — verify JWT has ADMIN role
        if (authHeader == null || !authHeader.startsWith("Bearer ")) {
            return ResponseEntity.status(401).body(
                    Map.of("error", "authentication_required",
                           "error_description", "Admin JWT required for client registration"));
        }
        try {
            var claims = io.jsonwebtoken.Jwts.parser()
                    .verifyWith(keyManager.getPublicKey())
                    .build()
                    .parseSignedClaims(authHeader.substring(7))
                    .getPayload();
            @SuppressWarnings("unchecked")
            var roles = (java.util.List<String>) claims.get("roles", java.util.List.class);
            if (roles == null || !roles.contains("ADMIN")) {
                return ResponseEntity.status(403).body(
                        Map.of("error", "forbidden",
                               "error_description", "ADMIN role required for client registration"));
            }
        } catch (Exception e) {
            return ResponseEntity.status(401).body(
                    Map.of("error", "invalid_token",
                           "error_description", "Invalid or expired admin token"));
        }

        String clientName = request.clientName() != null ? request.clientName() : request.clientId();
        String scopes = request.scopes() != null ? request.scopes() : "";
        var client = tokenService.registerClient(
                request.clientId(), request.clientSecret(), clientName, scopes, request.tenantId());
        return ResponseEntity.ok(Map.of(
                "client_id", client.getClientId(),
                "client_name", client.getClientName(),
                "scopes", client.getScopes(),
                "message", "Client registered successfully"));
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "UP", "service", "auth-service");
    }

    private void requireNonBlank(String value, String fieldName) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(fieldName + " is required");
        }
    }
}
