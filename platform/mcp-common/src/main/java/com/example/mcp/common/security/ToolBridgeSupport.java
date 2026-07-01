package com.example.mcp.common.security;

import java.util.Map;
import java.util.function.Supplier;

/**
 * Shared utility for ToolBridge controllers — extracts tenant lifecycle boilerplate.
 *
 * <p>Alibaba DRY: eliminates repeated ensureTenant + try/finally + clearTenant pattern
 * across memory-server, filesearch-server, and codeexec-server ToolBridgeControllers.</p>
 *
 * <p>Usage:</p>
 * <pre>
 * return ToolBridgeSupport.execute(() -> toolService.file_read(path));
 * </pre>
 */
public final class ToolBridgeSupport {

    private ToolBridgeSupport() {
    }

    /**
     * Execute a tool call with tenant context lifecycle management.
     *
     * @param action the tool action to execute (must return a String result)
     * @return Map with "result" key containing the tool output
     * @throws IllegalStateException if no tenant context is set
     */
    public static Map<String, String> execute(Supplier<String> action) {
        requireTenant();
        try {
            String result = action.get();
            return Map.of("result", result);
        } catch (IllegalArgumentException e) {
            return Map.of("result", "❌ " + e.getMessage());
        } finally {
            TenantContext.clear();
        }
    }

    /**
     * Require tenant context — reject unauthenticated requests.
     *
     * @throws IllegalStateException if no tenant context is set
     */
    public static void requireTenant() {
        if (TenantContext.getOrNull() == null) {
            throw new IllegalStateException("No tenant context — authentication required");
        }
    }
}
