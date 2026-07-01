package com.example.auth.security;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.util.Base64;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link RsaKeyManager} — covers the P0-1 rotation rewrite.
 *
 * <p>Crucially: these tests verify that {@code getJwks()} returns ALL active
 * kids (not just the signing one), which is what makes rotation possible
 * without invalidating every in-flight token.
 */
class RsaKeyManagerTest {

    // ── ephemeral mode (dev / back-compat) ──────────────────────

    @Test
    void should_generateSingleKey_when_ephemeralMode() {
        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.ephemeral);
        });

        assertNotNull(mgr.getPrivateKey(), "ephemeral mode must still expose a private key");
        assertNotNull(mgr.getPublicKey());
        assertNotNull(mgr.getKeyId());
        assertTrue(mgr.getKeyId().startsWith("eph-"),
                "ephemeral kid should be prefixed for ops clarity, got: " + mgr.getKeyId());

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> keys = (List<Map<String, Object>>) mgr.getJwks().get("keys");
        assertEquals(1, keys.size(), "ephemeral mode publishes exactly one key");
        assertEquals(mgr.getKeyId(), keys.get(0).get("kid"));
        assertEquals("RS256", keys.get(0).get("alg"));
        assertEquals("RSA", keys.get(0).get("kty"));
    }

    @Test
    void should_generateDistinctKids_when_twoEphemeralManagersStart() {
        RsaKeyManager a = newManager(p -> p.setMode(RsaKeyProperties.Mode.ephemeral));
        RsaKeyManager b = newManager(p -> p.setMode(RsaKeyProperties.Mode.ephemeral));

        // Regression guard: kids must include enough entropy that two restarts
        // don't collide (would silently bypass JWKS cache busting).
        assertNotEquals(a.getKeyId(), b.getKeyId(),
                "two ephemeral managers must produce distinct kids");
    }

    // ── file mode — single key ─────────────────────────────────

    @Test
    void should_loadSingleKey_when_filesPresent(@TempDir Path dir) throws Exception {
        writeKeyPair(dir, "k1");

        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.file);
            props.setKeysDir(dir.toString());
        });

        assertEquals("k1", mgr.getKeyId());
        assertNotNull(mgr.getPrivateKey());
        assertNotNull(mgr.getPublicKeyForKid("k1"));
        assertNull(mgr.getPublicKeyForKid("nope"), "unknown kid lookup must return null");
        assertNull(mgr.getPublicKeyForKid(null), "null kid lookup must not NPE");
    }

    @Test
    void should_failFast_when_modeFileButKeysDirMissing(@TempDir Path tmp) {
        Path nonExistent = tmp.resolve("does-not-exist");

        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> {
                    props.setMode(RsaKeyProperties.Mode.file);
                    props.setKeysDir(nonExistent.toString());
                }));
        assertTrue(ex.getMessage().contains("does not exist"),
                "must include actionable path in error: " + ex.getMessage());
    }

    @Test
    void should_failFast_when_modeFileButNoKeysDirConfigured() {
        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> props.setMode(RsaKeyProperties.Mode.file)));
        assertTrue(ex.getMessage().contains("auth.rsa.keys-dir"),
                "must reference the actual property name: " + ex.getMessage());
    }

    @Test
    void should_failFast_when_keysDirEmpty(@TempDir Path emptyDir) {
        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> {
                    props.setMode(RsaKeyProperties.Mode.file);
                    props.setKeysDir(emptyDir.toString());
                }));
        assertTrue(ex.getMessage().contains("No RSA key pairs"),
                "must explain WHY: " + ex.getMessage());
    }

    @Test
    void should_failFast_when_privateKeyHasNoMatchingPublicKey(@TempDir Path dir) throws Exception {
        // Write only the private half — simulates a partial deploy
        KeyPair pair = freshKeyPair();
        writePem(dir.resolve("k1.private.pem"), "PRIVATE KEY", pair.getPrivate().getEncoded());

        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> {
                    props.setMode(RsaKeyProperties.Mode.file);
                    props.setKeysDir(dir.toString());
                }));
        assertTrue(ex.getMessage().contains("Missing public key for kid 'k1'"),
                "must name the offending kid: " + ex.getMessage());
    }

    // ── file mode — multi-key rotation (THE core fix) ──────────

    @Test
    void should_publishAllKidsInJwks_when_multipleKeysPresent(@TempDir Path dir) throws Exception {
        // This is the key behavioural test for P0-1: pre-fix, only the *signing*
        // kid appeared in JWKS, so any rotation meant immediate mass logout because
        // downstream services couldn't verify in-flight tokens signed by the old key.
        writeKeyPair(dir, "2026-q2-old");
        writeKeyPair(dir, "2026-q3-new");

        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.file);
            props.setKeysDir(dir.toString());
            props.setSigningKid("2026-q3-new");
        });

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> keys = (List<Map<String, Object>>) mgr.getJwks().get("keys");
        assertEquals(2, keys.size(), "JWKS must contain BOTH kids during overlap window");

        // Signing kid must appear first (operational UX — downstream debugging)
        assertEquals("2026-q3-new", keys.get(0).get("kid"));
        assertEquals("2026-q2-old", keys.get(1).get("kid"));

        // Both must be independently usable
        assertNotNull(mgr.getPublicKeyForKid("2026-q2-old"),
                "old kid must remain queryable for in-flight token verification");
        assertNotNull(mgr.getPublicKeyForKid("2026-q3-new"));
        assertEquals(mgr.getPublicKeyForKid("2026-q3-new"), mgr.getPublicKey(),
                "getPublicKey() must return the signing kid's public key");
    }

    @Test
    void should_signWithConfiguredKid_when_signingKidExplicit(@TempDir Path dir) throws Exception {
        writeKeyPair(dir, "alpha");
        writeKeyPair(dir, "beta");
        writeKeyPair(dir, "gamma");

        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.file);
            props.setKeysDir(dir.toString());
            props.setSigningKid("beta");  // not the lexicographic max
        });

        assertEquals("beta", mgr.getKeyId(),
                "explicit signing-kid must override the lexicographic default");
    }

    @Test
    void should_pickLexicographicMax_when_signingKidNotConfigured(@TempDir Path dir) throws Exception {
        writeKeyPair(dir, "2026-q1");
        writeKeyPair(dir, "2026-q3");
        writeKeyPair(dir, "2026-q2");

        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.file);
            props.setKeysDir(dir.toString());
            // No signingKid configured
        });

        assertEquals("2026-q3", mgr.getKeyId(),
                "without explicit signing-kid, the lexicographically largest wins " +
                        "(date-sortable kids like 2026-q3 naturally select 'newest')");
    }

    @Test
    void should_failFast_when_signingKidNotFoundInDir(@TempDir Path dir) throws Exception {
        writeKeyPair(dir, "exists");

        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> {
                    props.setMode(RsaKeyProperties.Mode.file);
                    props.setKeysDir(dir.toString());
                    props.setSigningKid("typo");
                }));
        // Better than silently falling back — a typo in signing-kid would
        // otherwise mean every new token is signed with a key downstream
        // doesn't expect.
        assertTrue(ex.getMessage().contains("typo") && ex.getMessage().contains("exists"),
                "must list available kids to help operator: " + ex.getMessage());
    }

    // ── RFC 7518 compliance ───────────────────────────────────

    @Test
    void should_omitSignByte_when_encodingModulus(@TempDir Path dir) throws Exception {
        // RFC 7518 §6.3.1 — n must be unsigned big-endian. BigInteger.toByteArray()
        // prepends 0x00 when the high bit is set; downstream parsers reject this.
        // We don't need a special fixture: RSA-2048 moduli have their high bit set
        // about half the time, so generating a key and decoding it back is good enough.
        writeKeyPair(dir, "k1");

        RsaKeyManager mgr = newManager(props -> {
            props.setMode(RsaKeyProperties.Mode.file);
            props.setKeysDir(dir.toString());
        });

        @SuppressWarnings("unchecked")
        Map<String, Object> jwk = ((List<Map<String, Object>>) mgr.getJwks().get("keys")).get(0);
        byte[] nBytes = Base64.getUrlDecoder().decode((String) jwk.get("n"));
        // For a 2048-bit key, n must be exactly 256 bytes — never 257 (which is
        // what you get if you forget to strip the sign byte).
        assertEquals(256, nBytes.length,
                "RSA-2048 modulus must serialize to exactly 256 bytes, got " + nBytes.length);
    }

    @Test
    void should_encodeUnsignedBigInt_when_highBitSet() {
        // Direct unit test of the helper — feed it a value whose high bit IS set.
        // 1<<255 is a 256-bit number whose first byte is 0x80 (high bit set),
        // so BigInteger.toByteArray() prepends a 0x00 sign byte → 33 bytes total.
        BigInteger withSignBit = BigInteger.ONE.shiftLeft(255);
        byte[] javaForm = withSignBit.toByteArray();
        assertEquals(33, javaForm.length, "sanity: BigInteger emits 33 bytes with sign byte");
        assertEquals(0, javaForm[0], "sanity: leading byte is the 0x00 sign byte");
        assertEquals((byte) 0x80, javaForm[1], "sanity: second byte is the actual high byte");

        String encoded = RsaKeyManager.base64UrlUnsigned(withSignBit);
        byte[] decoded = Base64.getUrlDecoder().decode(encoded);
        assertEquals(32, decoded.length, "must strip the leading sign byte (33 → 32)");
        assertEquals((byte) 0x80, decoded[0],
                "after stripping, the first byte must be the original high byte");
    }

    // ── PEM loader edge cases ──────────────────────────────────

    @Test
    void should_giveActionableError_when_legacyPkcs1PrivateKey(@TempDir Path dir) throws IOException {
        // Anyone trying to use openssl's legacy default ("BEGIN RSA PRIVATE KEY")
        // hits a confusing InvalidKeySpecException from JDK. We pre-detect that
        // and tell them the openssl one-liner to fix it.
        Files.writeString(dir.resolve("legacy.private.pem"),
                "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBA...stub...\n-----END RSA PRIVATE KEY-----\n");
        // Pair with a real public key so we exercise the *private* failure path
        KeyPair pair = freshKeyPair();
        writePem(dir.resolve("legacy.public.pem"), "PUBLIC KEY", pair.getPublic().getEncoded());

        IllegalStateException ex = assertThrows(IllegalStateException.class, () ->
                newManager(props -> {
                    props.setMode(RsaKeyProperties.Mode.file);
                    props.setKeysDir(dir.toString());
                }));
        String msg = ex.getMessage();
        // Walk the cause chain — Spring may wrap our IllegalStateException
        Throwable t = ex;
        while (t != null && (msg == null || !msg.contains("pkcs8 -topk8"))) {
            t = t.getCause();
            if (t != null) msg = t.getMessage();
        }
        assertNotNull(msg, "expected PKCS#1 conversion hint somewhere in error chain");
        assertTrue(msg.contains("pkcs8 -topk8"),
                "must include the openssl conversion command: " + msg);
    }

    // ── helpers ────────────────────────────────────────────────

    private static RsaKeyManager newManager(java.util.function.Consumer<RsaKeyProperties> cfg) {
        RsaKeyProperties props = new RsaKeyProperties();
        cfg.accept(props);
        RsaKeyManager mgr = new RsaKeyManager(props);
        mgr.init();
        return mgr;
    }

    private static KeyPair freshKeyPair() {
        try {
            KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
            gen.initialize(2048);
            return gen.generateKeyPair();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static void writeKeyPair(Path dir, String kid) throws IOException {
        KeyPair pair = freshKeyPair();
        writePem(dir.resolve(kid + ".private.pem"), "PRIVATE KEY", pair.getPrivate().getEncoded());
        writePem(dir.resolve(kid + ".public.pem"), "PUBLIC KEY", pair.getPublic().getEncoded());
    }

    private static void writePem(Path file, String type, byte[] der) throws IOException {
        String body = Base64.getMimeEncoder(64, "\n".getBytes()).encodeToString(der);
        String pem = "-----BEGIN " + type + "-----\n" + body + "\n-----END " + type + "-----\n";
        Files.writeString(file, pem);
    }
}
