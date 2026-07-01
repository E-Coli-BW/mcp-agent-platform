package com.example.completion.filter;

import com.example.completion.config.CompletionProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;
import reactor.core.Disposable;
import reactor.core.publisher.Mono;

import java.util.concurrent.ConcurrentHashMap;

/**
 * Debounce filter for completion requests — the KEY performance feature.
 *
 * <h2>THE PROBLEM</h2>
 * Users type fast (~100ms between keystrokes), but LLM completions take 200-500ms.
 * Without debounce, typing "def calc" fires 8 requests, only the last one matters.
 * The first 7 waste Ollama GPU cycles and pile up in-flight connections.
 *
 * <h2>THE SOLUTION</h2>
 * When a new request arrives with the same X-Request-Id as an in-flight request,
 * we cancel the previous one via {@code Disposable.dispose()}.
 *
 * <h2>WHY JAVA/REACTOR IS BETTER THAN PYTHON FOR THIS</h2>
 * <ul>
 *   <li>{@code Disposable.dispose()} propagates the cancel signal UPSTREAM through
 *       the entire Reactor chain: Mono → WebClient → Netty → TCP connection to Ollama.
 *       The HTTP connection is actually aborted, freeing the Ollama GPU immediately.</li>
 *   <li>In Python, {@code asyncio.Task.cancel()} only raises CancelledError in the
 *       Python coroutine. The underlying HTTP connection (aiohttp/httpx) may NOT
 *       be closed — the request continues on the server side.</li>
 * </ul>
 *
 * <h2>THREADING MODEL</h2>
 * Spring WebFlux runs on Netty's event loop threads (default: CPU cores × 2).
 * This filter runs on the event loop — no blocking allowed.
 * {@code ConcurrentHashMap} provides thread-safe access across event loop threads.
 *
 * <h2>MEMORY MODEL</h2>
 * {@code inflight} map holds at most one Disposable per unique requestId.
 * Entries are removed in doFinally (normal completion) or onDispose (cancel).
 * Memory footprint: ~O(concurrent_users), typically <100 entries.
 *
 * <h2>HOW THE CANCEL CHAIN WORKS</h2>
 * <pre>
 * dispose() called on Disposable
 *   → Mono.create's sink.onDispose() fires
 *     → inner subscription disposed
 *       → WebClient's Flux body subscription cancelled
 *         → Netty channel closed (TCP RST sent to Ollama)
 *           → Ollama receives broken pipe, stops generating tokens
 * </pre>
 */
@Component
public class CompletionDebounceFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(CompletionDebounceFilter.class);

    // Maps requestId → Disposable of the in-flight request.
    // ConcurrentHashMap because Netty event loops are multi-threaded.
    // Using Disposable (not Mono) because we need to cancel the SUBSCRIPTION,
    // not the publisher. The subscription is what holds the active HTTP connection.
    private final ConcurrentHashMap<String, Disposable> inflight = new ConcurrentHashMap<>();
    
    private final boolean enabled;

    public CompletionDebounceFilter(CompletionProperties props) {
        this.enabled = props.getDebounce().isEnabled();
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        // Skip non-completion endpoints (health checks, etc.)
        if (!enabled) return chain.filter(exchange);

        String path = exchange.getRequest().getPath().value();
        if (!path.startsWith("/v1/completions")) {
            return chain.filter(exchange);
        }

        // X-Request-Id groups related requests (e.g., all keystrokes in one typing session).
        // The IDE sends the same ID for the same cursor position, incrementing on each keystroke.
        // If no ID provided, skip debounce (stateless request).
        String requestId = exchange.getRequest().getHeaders().getFirst("X-Request-Id");
        if (requestId == null) {
            return chain.filter(exchange);
        }

        // ── Cancel previous + register new (ATOMIC via compute()) ─────
        // WHY compute() INSTEAD OF get()+put()?
        // ConcurrentHashMap.get() + dispose() + put() is NOT atomic.
        // Two Netty event loop threads could both get() the same previous
        // Disposable, both dispose() it (double-dispose), and one put()
        // overwrites the other (lost request). compute() holds the segment
        // lock for the entire lambda, making the check-and-swap atomic.
        //
        // The flow:
        // 1. Mono.create() gives us a MonoSink
        // 2. We subscribe to chain.filter() manually (getting a Disposable)
        // 3. Inside compute(), we atomically dispose old + register new
        // 4. When dispose() is called later, it cancels the subscription
        // 5. doFinally runs on normal completion OR cancellation to clean up
        return Mono.create(sink -> {
            Disposable current = chain.filter(exchange)
                    // doFinally runs regardless of how the chain completes:
                    // - onComplete (normal finish)
                    // - onError (exception)
                    // - cancel (our dispose() called)
                    .doFinally(signal -> inflight.remove(requestId))
                    .subscribe(
                            unused -> {},      // onNext: Mono<Void> never emits items
                            sink::error,       // onError: propagate to outer Mono
                            sink::success      // onComplete: signal success to outer Mono
                    );
            
            // Atomic cancel-and-register using compute()
            // The lambda runs under segment lock — no race between threads
            inflight.compute(requestId, (key, prev) -> {
                if (prev != null && !prev.isDisposed()) {
                    log.debug("Cancelling previous completion for requestId={}", key);
                    prev.dispose();
                }
                return current;
            });

            // If the OUTER Mono is cancelled (e.g., client disconnects, browser tab closed),
            // we also need to cancel the inner subscription and clean up
            sink.onDispose(() -> {
                current.dispose();
                inflight.remove(requestId);
            });
        });
    }
}
