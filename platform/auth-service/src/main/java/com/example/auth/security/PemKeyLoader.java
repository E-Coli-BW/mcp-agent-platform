package com.example.auth.security;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.NoSuchAlgorithmException;
import java.security.spec.InvalidKeySpecException;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.security.interfaces.RSAPrivateKey;
import java.security.interfaces.RSAPublicKey;
import java.util.Base64;
import java.util.Locale;

/**
 * Minimal PKCS#8 / X.509 PEM loader for RSA keys.
 *
 * <p>Intentionally no BouncyCastle dependency — the PEM format is just a base64
 * wrapper around DER bytes, and JDK 21's {@link KeyFactory} understands both
 * PKCS#8 (private) and X.509 SubjectPublicKeyInfo (public) DER natively.
 *
 * <p>Supports the standard headers:
 * <ul>
 *   <li>{@code -----BEGIN PRIVATE KEY-----} (PKCS#8 unencrypted)</li>
 *   <li>{@code -----BEGIN PUBLIC KEY-----} (X.509 SPKI)</li>
 * </ul>
 *
 * <p>Does NOT support the legacy {@code -----BEGIN RSA PRIVATE KEY-----} PKCS#1
 * format — convert with {@code openssl pkcs8 -topk8 -nocrypt -in old.pem -out new.pem}.
 * This is a deliberate guardrail; PKCS#1 + JDK is a common foot-gun.
 */
final class PemKeyLoader {

    private PemKeyLoader() {}

    static RSAPrivateKey loadPrivate(Path pemFile) throws IOException {
        byte[] der = decodePem(Files.readString(pemFile),
                "PRIVATE KEY", pemFile.toString());
        try {
            KeyFactory kf = KeyFactory.getInstance("RSA");
            return (RSAPrivateKey) kf.generatePrivate(new PKCS8EncodedKeySpec(der));
        } catch (NoSuchAlgorithmException | InvalidKeySpecException e) {
            throw new IllegalStateException(
                    "Failed to decode RSA private key from " + pemFile
                            + " — expected PKCS#8 PEM (BEGIN PRIVATE KEY). "
                            + "Convert PKCS#1 with: openssl pkcs8 -topk8 -nocrypt -in <old.pem> -out <new.pem>",
                    e);
        }
    }

    static RSAPublicKey loadPublic(Path pemFile) throws IOException {
        byte[] der = decodePem(Files.readString(pemFile),
                "PUBLIC KEY", pemFile.toString());
        try {
            KeyFactory kf = KeyFactory.getInstance("RSA");
            return (RSAPublicKey) kf.generatePublic(new X509EncodedKeySpec(der));
        } catch (NoSuchAlgorithmException | InvalidKeySpecException e) {
            throw new IllegalStateException(
                    "Failed to decode RSA public key from " + pemFile
                            + " — expected X.509 SPKI PEM (BEGIN PUBLIC KEY)",
                    e);
        }
    }

    /**
     * Strip PEM headers/footers and base64-decode the body.
     *
     * @param pem        full file content (multi-line)
     * @param keyType    e.g. "PRIVATE KEY" or "PUBLIC KEY"
     * @param sourceHint file path for error messages
     */
    private static byte[] decodePem(String pem, String keyType, String sourceHint) {
        String header = "-----BEGIN " + keyType + "-----";
        String footer = "-----END " + keyType + "-----";

        int headerIdx = pem.indexOf(header);
        int footerIdx = pem.indexOf(footer);
        if (headerIdx < 0 || footerIdx <= headerIdx) {
            // Detect common mistake: legacy PKCS#1 file
            if (keyType.equals("PRIVATE KEY")
                    && pem.toUpperCase(Locale.ROOT).contains("RSA PRIVATE KEY")) {
                throw new IllegalStateException(
                        sourceHint + " is a legacy PKCS#1 (BEGIN RSA PRIVATE KEY) file. "
                                + "Convert with: openssl pkcs8 -topk8 -nocrypt -in "
                                + sourceHint + " -out <new.pem>");
            }
            throw new IllegalStateException(
                    sourceHint + " is not a valid PEM file — missing "
                            + header + " / " + footer);
        }

        String body = pem.substring(headerIdx + header.length(), footerIdx)
                .replaceAll("\\s+", "");
        try {
            return Base64.getDecoder().decode(body);
        } catch (IllegalArgumentException e) {
            throw new IllegalStateException(
                    sourceHint + " has malformed base64 body: " + e.getMessage(), e);
        }
    }
}
