package com.example.auth.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.stereotype.Component;

import jakarta.annotation.PostConstruct;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigInteger;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.interfaces.RSAPrivateKey;
import java.security.interfaces.RSAPublicKey;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Manages RSA key pair(s) for JWT signing (RS256) — now with rotation support.
 *
 * <h3>Why this is more than "just store a key"</h3>
 * The original implementation generated a fresh key on every startup and only
 * exposed the current key in {@code /auth/jwks}. Two production hazards:
 * <ol>
 *   <li><b>Restart = mass logout.</b> Every JWT in flight (and every refresh
 *       token, and every cached JWKS entry in every downstream service)
 *       becomes invalid the instant this pod restarts.</li>
 *   <li><b>Rotation is impossible.</b> Even if you load a persistent key,
 *       there's no overlap window where downstream services can verify
 *       tokens signed by the previous key — so any rotation = mass logout.</li>
 * </ol>
 *
 * <h3>Design</h3>
 * <ul>
 *   <li><b>ephemeral mode</b> (default for dev): unchanged behaviour, plus a
 *       LOUD warning so operators don't accidentally ship it.</li>
 *   <li><b>file mode</b>: load every {@code <kid>.private.pem} + matching
 *       {@code <kid>.public.pem} pair from {@code auth.rsa.keys-dir}. The
 *       configured {@code signing-kid} signs new tokens; <i>all</i> loaded
 *       kids are published in JWKS so tokens signed by the previous key
 *       still verify during the overlap window.</li>
 *   <li><b>Backwards-compatible API</b>: {@link #getPrivateKey()} /
 *       {@link #getPublicKey()} / {@link #getKeyId()} return the current
 *       signing key. {@link #getJwks()} returns <i>all</i> active keys.</li>
 * </ul>
 *
 * <h3>Operational runbook</h3>
 * To rotate:
 * <ol>
 *   <li>Generate a new key pair:
 *       {@code openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out 2026-q3.private.pem}
 *       then {@code openssl rsa -in 2026-q3.private.pem -pubout -out 2026-q3.public.pem}</li>
 *   <li>Drop both files in {@code auth.rsa.keys-dir}.</li>
 *   <li>Restart — JWKS now publishes both old + new, but {@code signing-kid}
 *       is still the old one. Downstream services pick up the new public key.</li>
 *   <li>After downstream JWKS cache TTL (5 min) has passed, set
 *       {@code auth.rsa.signing-kid} to the new kid and restart. New tokens
 *       are signed with the new key; old tokens still verify until expiry.</li>
 *   <li>After max-token-TTL (1 h) has passed, delete the old PEM and restart.</li>
 * </ol>
 */
@Component
@EnableConfigurationProperties(RsaKeyProperties.class)
public class RsaKeyManager {

    private static final Logger log = LoggerFactory.getLogger(RsaKeyManager.class);

    private final RsaKeyProperties props;

    /** Insertion-ordered: signing kid first, then verify-only kids. */
    private LinkedHashMap<String, RsaKey> keysByKid;

    /** Cached for fast JWKS serving — recomputed once in init(). */
    private List<Map<String, Object>> jwksKeys;

    /** The kid we sign new tokens with. */
    private String signingKid;

    public RsaKeyManager(RsaKeyProperties props) {
        this.props = props;
    }

    @PostConstruct
    public void init() {
        switch (props.getMode()) {
            case ephemeral -> initEphemeral();
            case file -> initFromFiles();
        }
        this.jwksKeys = buildJwksKeys();
        log.info("🔑 RsaKeyManager ready: mode={}, signingKid={}, totalKids={} ({})",
                props.getMode(), signingKid, keysByKid.size(), String.join(",", keysByKid.keySet()));
    }

    // ── Public API (back-compat) ────────────────────────────────

    public RSAPrivateKey getPrivateKey() { return keysByKid.get(signingKid).privateKey(); }

    public RSAPublicKey getPublicKey() { return keysByKid.get(signingKid).publicKey(); }

    public String getKeyId() { return signingKid; }

    /**
     * Look up the public key for an arbitrary kid (used by verification paths
     * that need to validate tokens signed by older keys during rotation).
     *
     * @return the public key, or {@code null} if no such kid is known
     */
    public RSAPublicKey getPublicKeyForKid(String kid) {
        if (kid == null) return null;
        RsaKey k = keysByKid.get(kid);
        return k == null ? null : k.publicKey();
    }

    /**
     * Returns the JWKS (JSON Web Key Set). Contains <b>every</b> active kid,
     * not just the signing key — this is what makes overlap-rotation work.
     */
    public Map<String, Object> getJwks() {
        return Map.of("keys", jwksKeys);
    }

    // ── init: ephemeral (dev) ───────────────────────────────────

    private void initEphemeral() {
        try {
            KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
            gen.initialize(2048);
            KeyPair pair = gen.generateKeyPair();
            String kid = "eph-" + UUID.randomUUID().toString().substring(0, 8);
            RsaKey key = new RsaKey(kid,
                    (RSAPrivateKey) pair.getPrivate(),
                    (RSAPublicKey) pair.getPublic());
            this.keysByKid = new LinkedHashMap<>();
            this.keysByKid.put(kid, key);
            this.signingKid = kid;
            log.warn("⚠️  RSA key in EPHEMERAL mode (kid={}). Every restart invalidates all outstanding JWTs. " +
                    "Set auth.rsa.mode=file and auth.rsa.keys-dir for production.", kid);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to generate ephemeral RSA key pair", e);
        }
    }

    // ── init: file (prod) ───────────────────────────────────────

    private void initFromFiles() {
        if (props.getKeysDir() == null || props.getKeysDir().isBlank()) {
            throw new IllegalStateException(
                    "auth.rsa.mode=file requires auth.rsa.keys-dir to be set");
        }
        Path dir = Paths.get(props.getKeysDir());
        if (!Files.isDirectory(dir)) {
            throw new IllegalStateException(
                    "auth.rsa.keys-dir does not exist or is not a directory: " + dir.toAbsolutePath());
        }
        LinkedHashMap<String, RsaKey> loaded = scanKeyDirectory(dir);
        if (loaded.isEmpty()) {
            throw new IllegalStateException(
                    "No RSA key pairs found in " + dir.toAbsolutePath()
                            + " — expected <kid>.private.pem + <kid>.public.pem pairs");
        }
        this.signingKid = pickSigningKid(loaded);
        // Re-order so signing kid comes first — small UX win for log/JWKS readability
        LinkedHashMap<String, RsaKey> reordered = new LinkedHashMap<>();
        reordered.put(signingKid, loaded.get(signingKid));
        loaded.forEach((kid, key) -> {
            if (!kid.equals(signingKid)) reordered.put(kid, key);
        });
        this.keysByKid = reordered;
    }

    /**
     * Scan {@code dir} for {@code <kid>.private.pem} files; for each one,
     * require a matching {@code <kid>.public.pem}. Returns kids in
     * lexicographic order (deterministic).
     */
    private LinkedHashMap<String, RsaKey> scanKeyDirectory(Path dir) {
        List<Path> privateFiles = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir, "*.private.pem")) {
            for (Path p : stream) privateFiles.add(p);
        } catch (IOException e) {
            throw new UncheckedIOException("Failed to scan " + dir, e);
        }
        privateFiles.sort(Comparator.comparing(p -> p.getFileName().toString()));

        LinkedHashMap<String, RsaKey> result = new LinkedHashMap<>();
        for (Path privatePath : privateFiles) {
            String fileName = privatePath.getFileName().toString();
            String kid = fileName.substring(0, fileName.length() - ".private.pem".length());
            Path publicPath = dir.resolve(kid + ".public.pem");
            if (!Files.exists(publicPath)) {
                throw new IllegalStateException(
                        "Missing public key for kid '" + kid + "': expected " + publicPath);
            }
            try {
                RSAPrivateKey priv = PemKeyLoader.loadPrivate(privatePath);
                RSAPublicKey pub = PemKeyLoader.loadPublic(publicPath);
                result.put(kid, new RsaKey(kid, priv, pub));
                log.info("🔑 Loaded RSA key pair: kid={}", kid);
            } catch (IOException e) {
                throw new UncheckedIOException("Failed to load key kid=" + kid, e);
            }
        }
        return result;
    }

    private String pickSigningKid(LinkedHashMap<String, RsaKey> loaded) {
        String configured = props.getSigningKid();
        if (configured != null && !configured.isBlank()) {
            if (!loaded.containsKey(configured)) {
                throw new IllegalStateException(
                        "auth.rsa.signing-kid='" + configured + "' is not present in keys-dir. " +
                                "Available: " + loaded.keySet());
            }
            return configured;
        }
        // No explicit signing kid → pick the lexicographically largest
        // (sorting kids by date like "2026-q3" makes "newest" win automatically).
        return loaded.keySet().stream()
                .max(Comparator.naturalOrder())
                .orElseThrow();
    }

    // ── JWKS serialization ──────────────────────────────────────

    private List<Map<String, Object>> buildJwksKeys() {
        List<Map<String, Object>> out = new ArrayList<>(keysByKid.size());
        for (RsaKey k : keysByKid.values()) {
            out.add(toJwk(k));
        }
        return Collections.unmodifiableList(out);
    }

    private static Map<String, Object> toJwk(RsaKey k) {
        // RFC 7518 §6.3.1 — n and e are base64url-encoded *unsigned* big-endian
        // representations of the modulus and public exponent. BigInteger.toByteArray()
        // emits a leading 0x00 sign byte whenever the high bit is set; we must strip it
        // or downstream parsers (Python jwcrypto, Node jose, etc.) will reject the key.
        String n = base64UrlUnsigned(k.publicKey().getModulus());
        String e = base64UrlUnsigned(k.publicKey().getPublicExponent());
        return Map.of(
                "kty", "RSA",
                "kid", k.kid(),
                "use", "sig",
                "alg", "RS256",
                "n", n,
                "e", e);
    }

    /** Base64url-encode a {@link BigInteger} as an unsigned big-endian byte sequence. */
    static String base64UrlUnsigned(BigInteger value) {
        byte[] bytes = value.toByteArray();
        if (bytes.length > 1 && bytes[0] == 0) {
            // Drop sign byte
            byte[] trimmed = new byte[bytes.length - 1];
            System.arraycopy(bytes, 1, trimmed, 0, trimmed.length);
            bytes = trimmed;
        }
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    // ── value type ──────────────────────────────────────────────

    /** An RSA key pair tagged with its kid. */
    public record RsaKey(String kid, RSAPrivateKey privateKey, RSAPublicKey publicKey) {}
}
