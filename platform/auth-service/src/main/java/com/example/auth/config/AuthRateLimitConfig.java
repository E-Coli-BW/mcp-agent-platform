package com.example.auth.config;

import jakarta.servlet.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.io.IOException;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Rate limiting filter for auth endpoints.
 *
 * <p>Alibaba Security Rule #7: must implement replay/abuse prevention.
 * Limits: 30 req/min per IP on /auth/login, /auth/signup, /auth/token, /auth/register.</p>
 */
@Configuration
public class AuthRateLimitConfig {

    private static final int MAX_REQUESTS_PER_MINUTE = 30;

    @Bean
    public FilterRegistrationBean<AuthRateLimitFilter> authRateLimitFilter() {
        var reg = new FilterRegistrationBean<>(new AuthRateLimitFilter());
        reg.addUrlPatterns("/auth/login", "/auth/signup", "/auth/token", "/auth/register");
        reg.setOrder(1);
        return reg;
    }

    static class AuthRateLimitFilter implements Filter {

        private static final Logger log = LoggerFactory.getLogger(AuthRateLimitFilter.class);
        private final ConcurrentHashMap<String, RateWindow> windows = new ConcurrentHashMap<>();

        @Override
        public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
                throws IOException, ServletException {
            HttpServletRequest req = (HttpServletRequest) request;
            String ip = req.getRemoteAddr();
            String key = ip + ":" + req.getRequestURI();

            RateWindow window = windows.compute(key, (k, existing) -> {
                long now = System.currentTimeMillis();
                if (existing == null || now - existing.windowStart > 60_000) {
                    return new RateWindow(now, new AtomicInteger(1));
                }
                existing.count.incrementAndGet();
                return existing;
            });

            if (window.count.get() > MAX_REQUESTS_PER_MINUTE) {
                log.warn("Rate limit exceeded: ip={}, path={}, count={}", ip, req.getRequestURI(), window.count.get());
                HttpServletResponse resp = (HttpServletResponse) response;
                resp.setStatus(429);
                resp.setContentType("application/json");
                resp.getWriter().write("{\"error\":\"rate_limit_exceeded\",\"error_description\":\"Too many requests. Max "
                        + MAX_REQUESTS_PER_MINUTE + " per minute.\"}");
                return;
            }

            chain.doFilter(request, response);
        }

        private static class RateWindow {
            final long windowStart;
            final AtomicInteger count;

            RateWindow(long windowStart, AtomicInteger count) {
                this.windowStart = windowStart;
                this.count = count;
            }
        }
    }
}
