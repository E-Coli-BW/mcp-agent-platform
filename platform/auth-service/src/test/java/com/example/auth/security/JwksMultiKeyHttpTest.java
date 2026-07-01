package com.example.auth.security;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.util.Base64;
import java.util.HashSet;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * End-to-end test: when the service is started in {@code file} mode with two
 * key pairs on disk, the {@code /auth/jwks} HTTP endpoint must publish BOTH
 * kids. This is the acceptance test for the P0-1 rotation fix — pre-fix, only
 * one key was published and any rotation caused mass logout.
 *
 * <p>Uses {@link DynamicPropertySource} to write temp PEM files BEFORE Spring
 * starts (the property registry hook is called during context preparation, so
 * we can't use {@code @TempDir} in the usual way).
 */
@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:auth-service-jwks-multikey-test;DB_CLOSE_DELAY=-1"
})
@AutoConfigureMockMvc
class JwksMultiKeyHttpTest {

    @TempDir(cleanup = org.junit.jupiter.api.io.CleanupMode.ON_SUCCESS)
    static Path KEYS_DIR;

    @Autowired
    private MockMvc mockMvc;

    @BeforeAll
    static void writeKeyPairs() throws Exception {
        // Two pairs: kid `2026-q2-old` (legacy) and `2026-q3-new` (current signer)
        writeKeyPair(KEYS_DIR, "2026-q2-old");
        writeKeyPair(KEYS_DIR, "2026-q3-new");
    }

    @DynamicPropertySource
    static void rsaProps(DynamicPropertyRegistry registry) {
        registry.add("auth.rsa.mode", () -> "file");
        registry.add("auth.rsa.keys-dir", KEYS_DIR::toString);
        registry.add("auth.rsa.signing-kid", () -> "2026-q3-new");
    }

    @Test
    void jwksEndpoint_publishesAllKids_forRotationOverlap() throws Exception {
        MvcResult result = mockMvc.perform(get("/auth/jwks"))
                .andExpect(status().isOk())
                .andReturn();

        JsonNode body = new ObjectMapper().readTree(result.getResponse().getContentAsString());
        JsonNode keys = body.get("keys");
        assertNotNull(keys, "JWKS response must have a 'keys' array");
        assertEquals(2, keys.size(),
                "JWKS must publish BOTH kids during rotation overlap, got: " + keys.toPrettyString());

        Set<String> kids = new HashSet<>();
        for (JsonNode key : keys) {
            kids.add(key.get("kid").asText());
            assertEquals("RSA", key.get("kty").asText());
            assertEquals("RS256", key.get("alg").asText());
            assertEquals("sig", key.get("use").asText());
            // base64url-encoded modulus for RSA-2048 must be ~342 chars (256 bytes → 342 base64url)
            String n = key.get("n").asText();
            byte[] nBytes = Base64.getUrlDecoder().decode(n);
            assertEquals(256, nBytes.length,
                    "Each modulus must be exactly 256 bytes (RSA-2048, no sign byte)");
        }
        assertEquals(Set.of("2026-q2-old", "2026-q3-new"), kids,
                "Both kids must appear in JWKS — old one for in-flight token verification, " +
                        "new one for fresh tokens");

        // Signing kid must be first (operational UX: easy `jq '.keys[0]'` for current signer)
        assertEquals("2026-q3-new", keys.get(0).get("kid").asText(),
                "Signing kid must be first in JWKS output");
    }

    // ── helpers ────────────────────────────────────────────────

    private static void writeKeyPair(Path dir, String kid) throws Exception {
        KeyPairGenerator gen = KeyPairGenerator.getInstance("RSA");
        gen.initialize(2048);
        KeyPair pair = gen.generateKeyPair();
        writePem(dir.resolve(kid + ".private.pem"), "PRIVATE KEY", pair.getPrivate().getEncoded());
        writePem(dir.resolve(kid + ".public.pem"), "PUBLIC KEY", pair.getPublic().getEncoded());
    }

    private static void writePem(Path file, String type, byte[] der) throws IOException {
        String body = Base64.getMimeEncoder(64, "\n".getBytes()).encodeToString(der);
        String pem = "-----BEGIN " + type + "-----\n" + body + "\n-----END " + type + "-----\n";
        Files.writeString(file, pem);
    }
}
