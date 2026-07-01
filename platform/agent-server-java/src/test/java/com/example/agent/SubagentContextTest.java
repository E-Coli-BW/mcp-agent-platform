package com.example.agent;

import com.example.agent.agent.SubagentContext;
import com.example.agent.agent.SubagentContext.SpawnRejectedException;
import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Tests for SubagentContext — fleet governance budget envelope.
 */
class SubagentContextTest {

    @Test
    void should_createRootContext_when_initialized() {
        SubagentContext ctx = SubagentContext.root("session-1", Set.of("file_read", "memory_search"));

        assertThat(ctx.rootSessionId()).isEqualTo("session-1");
        assertThat(ctx.depth()).isZero();
        assertThat(ctx.fanoutUsed()).isZero();
        assertThat(ctx.tokensRemaining()).isEqualTo(SubagentContext.DEFAULT_BUDGET_TOKENS);
        assertThat(ctx.allowedTools()).containsExactlyInAnyOrder("file_read", "memory_search");
        assertThat(ctx.remainingMs()).isGreaterThan(0);
    }

    @Test
    void should_deriveChild_when_policySatisfied() {
        SubagentContext parent = SubagentContext.root("s1", Set.of("file_read"));

        SubagentContext child = parent.deriveChild("s1/sub-abc", Set.of("file_read"), 5000);

        assertThat(child.depth()).isEqualTo(1);
        assertThat(child.fanoutUsed()).isZero(); // child gets fresh fanout
        assertThat(child.tokensRemaining()).isEqualTo(parent.tokensRemaining() - 5000);
        assertThat(child.rootSessionId()).isEqualTo("s1");
    }

    @Test
    void should_rejectSpawn_when_depthExceeded() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"));
        // Simulate depth=3 by chaining derives
        SubagentContext d1 = ctx.deriveChild("c1", Set.of("file_read"), 1000);
        SubagentContext d2 = d1.deriveChild("c2", Set.of("file_read"), 1000);
        SubagentContext d3 = d2.deriveChild("c3", Set.of("file_read"), 1000);

        // depth=3 → deriving another should fail
        assertThatThrownBy(() -> d3.deriveChild("c4", Set.of("file_read"), 1000))
                .isInstanceOf(SpawnRejectedException.class)
                .hasMessageContaining("depth limit exceeded");
    }

    @Test
    void should_rejectSpawn_when_fanoutExceeded() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"));

        // Simulate 8 spawns by incrementing fanout
        SubagentContext parent = ctx;
        for (int i = 0; i < SubagentContext.MAX_FANOUT_CEILING; i++) {
            parent = parent.withFanoutIncremented();
        }

        SubagentContext maxedOut = parent;
        assertThatThrownBy(() -> maxedOut.deriveChild("c9", Set.of("file_read"), 1000))
                .isInstanceOf(SpawnRejectedException.class)
                .hasMessageContaining("fanout limit exceeded");
    }

    @Test
    void should_rejectSpawn_when_tokenBudgetExhausted() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"), 100, 120_000);

        assertThatThrownBy(() -> ctx.deriveChild("c1", Set.of("file_read"), 200))
                .isInstanceOf(SpawnRejectedException.class)
                .hasMessageContaining("token budget exhausted");
    }

    @Test
    void should_rejectSpawn_when_toolsNotInAllowlist() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"));

        assertThatThrownBy(() -> ctx.deriveChild("c1", Set.of("file_read", "code_run"), 1000))
                .isInstanceOf(SpawnRejectedException.class)
                .hasMessageContaining("not in the parent's allowlist");
    }

    @Test
    void should_rejectSpawn_when_noAllowlistSet() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of());

        assertThatThrownBy(() -> ctx.deriveChild("c1", Set.of("file_read"), 1000))
                .isInstanceOf(SpawnRejectedException.class)
                .hasMessageContaining("not enabled for this request");
    }

    @Test
    void should_trackFanout_when_incrementCalled() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"));
        assertThat(ctx.fanoutUsed()).isZero();

        SubagentContext updated = ctx.withFanoutIncremented();
        assertThat(updated.fanoutUsed()).isEqualTo(1);
        assertThat(ctx.fanoutUsed()).isZero(); // original unchanged (immutable)
    }

    @Test
    void should_settleTokens_when_consumedCalled() {
        SubagentContext ctx = SubagentContext.root("s1", Set.of("file_read"));
        int initial = ctx.tokensRemaining();

        SubagentContext settled = ctx.withTokensConsumed(5000);
        assertThat(settled.tokensRemaining()).isEqualTo(initial - 5000);
        assertThat(ctx.tokensRemaining()).isEqualTo(initial); // immutable
    }
}

