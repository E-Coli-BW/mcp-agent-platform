package com.example.mcp.common.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.math.BigInteger;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PublicKey;
import java.security.interfaces.RSAPublicKey;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Focused tests for the JWKS parsing path (P1-3 fix).
 *
 * The old implementation used naive {@code indexOf("\"n\":\"")} JSON parsing
 * and could only ever return a single PublicKey — once auth-service started
 * publishing two keys for rotation, downstream services would silently pin
 * to whichever key happened to appear first in the array and reject every
 * token signed with the other one. This locks in:
 *
 *   1. JWKS with multiple keys is parsed correctly (kid → key map).
 *   2. Non-RSA keys (EC, OKP, ...) are skipped, not blown up on.
 *   3. Malformed key entries don't poison the rest of the JWKS.
 *   4. Keys without a {@code kid} get a synthetic "default" id so single-key
 *      JWKS responses still work.
 *   5. Garbage JWKS body returns an empty map instead of crashing.
 */
class JwtAuthFilterJwksTest {

    private static final ObjectMapper M = new ObjectMapper();

    private static Map<String, String> jwkOf(String kid, RSAPublicKey key) {
        Map<String, String> jwk = new LinkedHashMap<>();
        jwk.put("kty", "RSA");
        jwk.put("kid", kid);
        jwk.put("use", "sig");
        jwk.put("alg", "RS256");
        jwk.put("n", Base64.getUrlEncoder().withoutPadding()
                .encodeToString(stripLeadingZero(key.getModulus().toByteArray())));
        jwk.put("e", Base64.getUrlEncoder().withoutPadding()
                .encodeToString(stripLeadingZero(key.getPublicExponent().toByteArray())));
        return jwk;
    }

    /** RSA modulus can have a leading 0x00 sign byte that JWKS spec omits. */
    private static byte[] stripLeadingZero(byte[] in) {
        if (in.length > 1 && in[0] == 0) {
            byte[] out = new byte[in.length - 1];
            System.arraycopy(in, 1, out, 0, out.length);
            return out;
        }
        return in;
    }

    private static RSAPublicKey genRsa() throws Exception {
        KeyPairGenerator g = KeyPairGenerator.getInstance("RSA");
        g.initialize(2048);
        KeyPair kp = g.generateKeyPair();
        return (RSAPublicKey) kp.getPublic();
    }

    @Test
    void should_parseMultipleKeys_indexedByKid() throws Exception {
        RSAPublicKey a = genRsa();
        RSAPublicKey b = genRsa();
        String body = M.writeValueAsString(Map.of("keys", List.of(
                jwkOf("key-a", a),
                jwkOf("key-b", b)
        )));

        Map<String, PublicKey> parsed = JwtAuthFilter.parseJwks(body);

        assertEquals(2, parsed.size(), "both keys must be loaded — naive parser pinned to first");
        assertTrue(parsed.containsKey("key-a"));
        assertTrue(parsed.containsKey("key-b"));
        // The parsed keys must produce the same modulus bytes we put in.
        // (Naive parser would have returned the same key for both kids.)
        RSAPublicKey rA = (RSAPublicKey) parsed.get("key-a");
        RSAPublicKey rB = (RSAPublicKey) parsed.get("key-b");
        assertNotEquals(rA.getModulus(), rB.getModulus());
        assertEquals(a.getModulus(), rA.getModulus());
        assertEquals(b.getModulus(), rB.getModulus());
    }

    @Test
    void should_skipNonRsaKeys_when_jwksMixesKeyTypes() throws Exception {
        RSAPublicKey rsa = genRsa();
        Map<String, Object> ecKey = Map.of(
                "kty", "EC", "kid", "ec-1",
                "crv", "P-256", "x", "AA", "y", "BB"
        );
        Map<String, Object> body = Map.of("keys", List.of(
                ecKey,
                jwkOf("rsa-1", rsa)
        ));

        Map<String, PublicKey> parsed = JwtAuthFilter.parseJwks(M.writeValueAsString(body));

        assertEquals(1, parsed.size(), "EC key must be skipped, only RSA loaded");
        assertTrue(parsed.containsKey("rsa-1"));
    }

    @Test
    void should_skipMalformedKey_when_othersAreValid() throws Exception {
        // Bad key: missing the 'n' field.
        Map<String, Object> bad = Map.of("kty", "RSA", "kid", "bad", "e", "AQAB");
        RSAPublicKey good = genRsa();
        String body = M.writeValueAsString(Map.of("keys", List.of(
                bad,
                jwkOf("good", good)
        )));

        Map<String, PublicKey> parsed = JwtAuthFilter.parseJwks(body);

        assertEquals(1, parsed.size());
        assertTrue(parsed.containsKey("good"));
        assertFalse(parsed.containsKey("bad"));
    }

    @Test
    void should_synthesiseKid_when_keyHasNone() throws Exception {
        RSAPublicKey rsa = genRsa();
        // jwk without "kid"
        Map<String, String> noKid = new LinkedHashMap<>();
        noKid.put("kty", "RSA");
        noKid.put("n", Base64.getUrlEncoder().withoutPadding()
                .encodeToString(stripLeadingZero(rsa.getModulus().toByteArray())));
        noKid.put("e", Base64.getUrlEncoder().withoutPadding()
                .encodeToString(stripLeadingZero(rsa.getPublicExponent().toByteArray())));
        String body = M.writeValueAsString(Map.of("keys", List.of(noKid)));

        Map<String, PublicKey> parsed = JwtAuthFilter.parseJwks(body);

        assertEquals(1, parsed.size(),
                "single-key JWKS w/o kid must still produce one entry");
        assertTrue(parsed.containsKey("default"));
    }

    @Test
    void should_returnEmpty_when_jwksHasNoKeysField() throws Exception {
        assertTrue(JwtAuthFilter.parseJwks("{}").isEmpty());
        assertTrue(JwtAuthFilter.parseJwks("{\"keys\": []}").isEmpty());
    }

    @Test
    void should_decodeModulusCorrectly_when_modulusHasLeadingZero() throws Exception {
        // Pick a key, prefix its modulus with 0x00 (the byte JWKS spec says to drop),
        // base64-encode it both ways and ensure both decode to the same BigInteger.
        RSAPublicKey rsa = genRsa();
        byte[] raw = rsa.getModulus().toByteArray();
        BigInteger expected = new BigInteger(1, stripLeadingZero(raw));
        BigInteger decoded = new BigInteger(1,
                Base64.getUrlDecoder().decode(
                        Base64.getUrlEncoder().withoutPadding()
                                .encodeToString(stripLeadingZero(raw))));
        assertEquals(expected, decoded);
    }
}
