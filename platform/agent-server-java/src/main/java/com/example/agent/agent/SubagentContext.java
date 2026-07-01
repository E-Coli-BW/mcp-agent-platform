package com.example.agent.agent;

import java.util.Set;

/**
 * SubagentContext — per-request fleet governance state.
 *
 * <p>This is the single source of truth for "can this agent spawn another agent right now?"
 * decisions. The budget envelope and depth fence keep the fleet from melting credit cards.</p>
 *
 * <p>Immutable by design — to update, create a new instance via the builder-style methods.
 * This mirrors the Python implementation where frozen dataclasses prevent accidental mutation
 * across async tasks.</p>
 *
 * <p>Tunables (hard ceilings the system will NEVER cross):
 * <ul>
 *   <li>{@code MAX_DEPTH_CEILING = 3} — absolute nesting depth limit</li>
 *   <li>{@code MAX_FANOUT_CEILING = 8} — max children per parent per request</li>
 *   <li>{@code DEFAULT_BUDGET_TOKENS = 60,000} — shared token budget for the fleet</li>
 *   <li>{@code DEFAULT_DEADLINE_MS = 120,000} — wallclock deadline from request start</li>
 * </ul>
 */
public record SubagentContext(
        String rootSessionId,
        String parentSessionId,
        int depth,
        int fanoutUsed,
        int tokensRemaining,
        long deadlineUnixMs,
        Set<String> allowedTools,
        int tokensUsedSelf
) {

    public static final int MAX_DEPTH_CEILING = 3;
    public static final int MAX_FANOUT_CEILING = 8;
    public static final int DEFAULT_BUDGET_TOKENS = 60_000;
    public static final int DEFAULT_DEADLINE_MS = 120_000;

    /**
     * Create a root context at the start of a user request.
     */
    public static SubagentContext root(String rootSessionId, Set<String> allowedTools) {
        return root(rootSessionId, allowedTools, DEFAULT_BUDGET_TOKENS, DEFAULT_DEADLINE_MS);
    }

    /**
     * Create a root context with explicit budget parameters.
     */
    public static SubagentContext root(String rootSessionId, Set<String> allowedTools,
                                       int tokenBudget, int deadlineMs) {
        return new SubagentContext(
                rootSessionId,
                rootSessionId,
                0,
                0,
                tokenBudget,
                System.currentTimeMillis() + deadlineMs,
                allowedTools,
                0
        );
    }

    /**
     * Derive a child context for a spawn attempt.
     *
     * @param childSessionId session id assigned to the child
     * @param requestedTools tools the parent wants the child to have
     * @param estimatedTokens optimistic token estimate for budget reservation
     * @return the child context
     * @throws SpawnRejectedException if any policy is violated
     */
    public SubagentContext deriveChild(String childSessionId, Set<String> requestedTools,
                                       int estimatedTokens) {
        // Policy 1: depth ceiling
        if (depth + 1 > MAX_DEPTH_CEILING) {
            throw new SpawnRejectedException(
                    "depth limit exceeded: parent at depth=" + depth
                            + ", max nesting is " + MAX_DEPTH_CEILING
                            + ". Restructure the work to avoid deeper recursion.");
        }

        // Policy 2: fanout ceiling
        if (fanoutUsed + 1 > MAX_FANOUT_CEILING) {
            throw new SpawnRejectedException(
                    "fanout limit exceeded: parent has already spawned "
                            + fanoutUsed + " children at this level, max is "
                            + MAX_FANOUT_CEILING + ". Batch the work into fewer calls.");
        }

        // Policy 3: token budget
        if (estimatedTokens > tokensRemaining) {
            throw new SpawnRejectedException(
                    "token budget exhausted: " + tokensRemaining + " tokens left "
                            + "but child estimated to need " + estimatedTokens
                            + ". Answer directly instead of spawning.");
        }

        // Policy 4: wallclock deadline
        if (remainingMs() <= 0) {
            throw new SpawnRejectedException(
                    "wallclock deadline exceeded — cannot spawn more subagents.");
        }

        // Policy 5: tool allowlist must be a SUBSET of parent's
        if (!allowedTools.isEmpty()) {
            Set<String> disallowed = new java.util.HashSet<>(requestedTools);
            disallowed.removeAll(allowedTools);
            if (!disallowed.isEmpty()) {
                throw new SpawnRejectedException(
                        "requested tools " + disallowed + " are not in the parent's "
                                + "allowlist. A subagent can never have MORE permissions.");
            }
        } else {
            throw new SpawnRejectedException(
                    "subagent spawning is not enabled for this request.");
        }

        return new SubagentContext(
                rootSessionId,
                parentSessionId,
                depth + 1,
                0, // child gets fresh fanout budget
                tokensRemaining - estimatedTokens,
                deadlineUnixMs,
                allowedTools,
                0
        );
    }

    /**
     * Return a new context with fanout incremented (called after successful spawn).
     */
    public SubagentContext withFanoutIncremented() {
        return new SubagentContext(rootSessionId, parentSessionId, depth,
                fanoutUsed + 1, tokensRemaining, deadlineUnixMs, allowedTools, tokensUsedSelf);
    }

    /**
     * Return a new context with tokens consumed by a child settled.
     */
    public SubagentContext withTokensConsumed(int tokensUsed) {
        return new SubagentContext(rootSessionId, parentSessionId, depth,
                fanoutUsed, Math.max(0, tokensRemaining - tokensUsed), deadlineUnixMs,
                allowedTools, tokensUsedSelf);
    }

    /**
     * Milliseconds remaining until the wallclock deadline.
     */
    public long remainingMs() {
        if (deadlineUnixMs <= 0) {
            return DEFAULT_DEADLINE_MS;
        }
        return Math.max(0, deadlineUnixMs - System.currentTimeMillis());
    }

    /**
     * Exception thrown when a spawn violates the fleet envelope.
     * The message is human-readable because the LLM will see it.
     */
    public static class SpawnRejectedException extends RuntimeException {
        public SpawnRejectedException(String message) {
            super(message);
        }
    }
}

