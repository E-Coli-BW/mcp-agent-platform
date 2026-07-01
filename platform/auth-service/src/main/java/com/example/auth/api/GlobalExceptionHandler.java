package com.example.auth.api;

import com.example.auth.service.RefreshTokenService;
import com.example.auth.service.TokenService;
import com.example.auth.service.UserService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.Map;

/**
 * Global exception handler — centralizes error formatting for all auth endpoints.
 *
 * <p>Alibaba Exception Handling #13 (DRY): replaces 9+ duplicated try-catch blocks in AuthController.</p>
 * <p>Security (S1): never exposes e.getMessage() — uses fixed messages instead.</p>
 */
@RestControllerAdvice(basePackages = "com.example.auth.api")
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(UserService.InvalidCredentialsException.class)
    public ResponseEntity<Map<String, String>> handleInvalidCredentials(UserService.InvalidCredentialsException e) {
        // NOTE: e.getMessage() may already be specific (e.g. "wrong password") for
        // logging purposes upstream, but the WIRE response stays generic on purpose:
        // separating "user not found" from "wrong password" leaks user existence.
        log.warn("Invalid credentials: {}", e.getMessage());
        return ResponseEntity.status(401).body(
                Map.of("error", AuthErrorCode.INVALID_CREDENTIALS,
                       "error_description", "Invalid username or password"));
    }

    @ExceptionHandler(UserService.RateLimitedException.class)
    public ResponseEntity<Map<String, String>> handleRateLimited(UserService.RateLimitedException e) {
        log.warn("Rate limited: {}", e.getMessage());
        return ResponseEntity.status(429).body(
                Map.of("error", AuthErrorCode.RATE_LIMITED,
                       "error_description", "Too many login attempts. Try again later."));
    }

    @ExceptionHandler(UserService.AccountLockedException.class)
    public ResponseEntity<Map<String, String>> handleAccountLocked(UserService.AccountLockedException e) {
        log.warn("Account locked: {}", e.getMessage());
        // 423 Locked — RFC 4918. Distinct from 401 (credentials) and 429 (rate limit).
        return ResponseEntity.status(423).body(
                Map.of("error", AuthErrorCode.ACCOUNT_LOCKED,
                       "error_description", "Account temporarily locked due to too many failed attempts. "
                               + "Try again in 5 minutes."));
    }

    @ExceptionHandler(UserService.AccountDisabledException.class)
    public ResponseEntity<Map<String, String>> handleAccountDisabled(UserService.AccountDisabledException e) {
        log.warn("Account disabled: {}", e.getMessage());
        return ResponseEntity.status(403).body(
                Map.of("error", AuthErrorCode.ACCOUNT_DISABLED,
                       "error_description", "Account is disabled. Contact an administrator."));
    }

    @ExceptionHandler(UserService.DuplicateUserException.class)
    public ResponseEntity<Map<String, String>> handleDuplicateUser(UserService.DuplicateUserException e) {
        return ResponseEntity.status(409).body(
                Map.of("error", "duplicate_user", "error_description", "Username already exists"));
    }

    @ExceptionHandler(UserService.UserException.class)
    public ResponseEntity<Map<String, String>> handleUserException(UserService.UserException e) {
        // Catch-all for any future UserException subtype that doesn't have a dedicated handler.
        // Should rarely fire — its existence keeps a previously broken request from leaking
        // an exception name to the client.
        log.warn("User operation failed: {}", e.getMessage());
        return ResponseEntity.status(403).body(
                Map.of("error", AuthErrorCode.FORBIDDEN, "error_description", "Operation not permitted"));
    }

    @ExceptionHandler(TokenService.AuthException.class)
    public ResponseEntity<Map<String, String>> handleAuthException(TokenService.AuthException e) {
        log.warn("Token auth failed: {}", e.getMessage());
        return ResponseEntity.status(401).body(
                Map.of("error", AuthErrorCode.INVALID_CLIENT, "error_description", "Authentication failed"));
    }

    @ExceptionHandler(TokenService.DuplicateClientException.class)
    public ResponseEntity<Map<String, String>> handleDuplicateClient(TokenService.DuplicateClientException e) {
        return ResponseEntity.status(409).body(
                Map.of("error", "duplicate_client", "error_description", "Client already exists"));
    }

    @ExceptionHandler(RefreshTokenService.InvalidRefreshTokenException.class)
    public ResponseEntity<Map<String, String>> handleInvalidRefreshToken(RefreshTokenService.InvalidRefreshTokenException e) {
        return ResponseEntity.status(401).body(
                Map.of("error", AuthErrorCode.INVALID_TOKEN, "error_description", "Invalid or expired refresh token"));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, String>> handleValidation(MethodArgumentNotValidException e) {
        String field = e.getBindingResult().getFieldErrors().stream()
                .map(err -> err.getField() + ": " + err.getDefaultMessage())
                .findFirst().orElse("Invalid input");
        return ResponseEntity.badRequest().body(
                Map.of("error", AuthErrorCode.INVALID_REQUEST, "error_description", field));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, String>> handleIllegalArgument(IllegalArgumentException e) {
        return ResponseEntity.badRequest().body(
                Map.of("error", AuthErrorCode.INVALID_REQUEST, "error_description", "Invalid request parameters"));
    }
}
