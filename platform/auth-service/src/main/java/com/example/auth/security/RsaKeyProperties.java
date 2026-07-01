package com.example.auth.security;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * RSA key management configuration.
 *
 * <p>Two modes:
 * <ul>
 *   <li><b>ephemeral</b> (default) — generate a fresh RSA-2048 pair on startup.
 *       Convenient for dev, but every restart invalidates every outstanding
 *       JWT. A loud WARN is logged on startup.</li>
 *   <li><b>file</b> — load PKCS#8 PEM key pairs from {@link #keysDir}. Each
 *       key pair is two files: {@code &lt;kid&gt;.private.pem} and
 *       {@code &lt;kid&gt;.public.pem}. Use {@link #signingKid} to pick which
 *       kid signs new tokens; all loaded kids are published in JWKS so
 *       in-flight tokens signed by older kids still verify (rotation overlap).
 *       Missing or malformed PEM is a fail-fast at boot — we never silently
 *       fall back to ephemeral in prod.</li>
 * </ul>
 */
@ConfigurationProperties(prefix = "auth.rsa")
public class RsaKeyProperties {

    public enum Mode { ephemeral, file }

    /** Source of RSA keys. */
    private Mode mode = Mode.ephemeral;

    /** Directory containing {@code <kid>.private.pem} + {@code <kid>.public.pem} pairs. */
    private String keysDir;

    /**
     * Which kid is the current signing key. New tokens are signed with this kid.
     * If null/blank, the lexicographically largest kid wins (deterministic).
     * Older kids in {@link #keysDir} remain in JWKS for verification only.
     */
    private String signingKid;

    public Mode getMode() { return mode; }
    public void setMode(Mode mode) { this.mode = mode; }

    public String getKeysDir() { return keysDir; }
    public void setKeysDir(String keysDir) { this.keysDir = keysDir; }

    public String getSigningKid() { return signingKid; }
    public void setSigningKid(String signingKid) { this.signingKid = signingKid; }
}
