package com.example.agent.api;

import com.example.agent.workspace.WorkspaceResolver;
import com.example.mcp.common.security.TenantContext;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.reactive.AutoConfigureWebTestClient;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.test.web.reactive.server.WebTestClient;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.server.ServerWebExchange;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertEquals;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureWebTestClient
class WorkspaceControllerTest {

    @Autowired
    private WebTestClient webTestClient;

    private Path workspace;

    @BeforeEach
    void openWorkspace() {
        workspace = Path.of("src/test/resources/workspace").toAbsolutePath().normalize();
        webTestClient.post()
                .uri("/api/workspace/open")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(Map.of("path", workspace.toString()))
                .exchange()
                .expectStatus().isOk()
                .expectBody()
                .jsonPath("$.path").isEqualTo(workspace.toString());
    }

    @Test
    void shouldListFiles() {
        webTestClient.get()
                .uri("/api/workspace/files")
                .accept(MediaType.APPLICATION_JSON)
                .exchange()
                .expectStatus().isOk()
                .expectBody()
                .jsonPath("$.root").isEqualTo(workspace.toString())
                .jsonPath("$.tree[0].name").isEqualTo("sample.txt");
    }

    @Test
    void shouldReadFileRange() {
        webTestClient.get()
                .uri(uriBuilder -> uriBuilder.path("/api/workspace/file")
                        .queryParam("path", "sample.txt")
                        .queryParam("startLine", 2)
                        .queryParam("endLine", 3)
                        .build())
                .exchange()
                .expectStatus().isOk()
                .expectBody()
                .jsonPath("$.content").isEqualTo("beta\ngamma")
                .jsonPath("$.startLine").isEqualTo(2)
                .jsonPath("$.endLine").isEqualTo(3);
    }

    @Test
    void should_isolateWorkspaces_when_twoTenantsCallOpenConcurrently(@TempDir Path tempDir) throws Exception {
        WorkspaceController controller = new WorkspaceController(
                new WorkspaceResolver(Files.createDirectories(tempDir.resolve("base")).toString(), true));
        Path tenantOneWorkspace = Files.createDirectories(tempDir.resolve("tenant-one-workspace"));
        Path tenantTwoWorkspace = Files.createDirectories(tempDir.resolve("tenant-two-workspace"));
        Files.writeString(tenantOneWorkspace.resolve("one.txt"), "one");
        Files.writeString(tenantTwoWorkspace.resolve("two.txt"), "two");

        ExecutorService executorService = Executors.newFixedThreadPool(2);
        CountDownLatch startLatch = new CountDownLatch(1);
        try {
            Future<?> tenantOne = executorService.submit(() -> {
                await(startLatch);
                openWorkspaceForTenant(controller, "tenant-one", tenantOneWorkspace);
            });
            Future<?> tenantTwo = executorService.submit(() -> {
                await(startLatch);
                openWorkspaceForTenant(controller, "tenant-two", tenantTwoWorkspace);
            });

            startLatch.countDown();
            tenantOne.get(10, TimeUnit.SECONDS);
            tenantTwo.get(10, TimeUnit.SECONDS);
        } finally {
            executorService.shutdownNow();
        }

        Map<String, Object> tenantOneFile = withTenant("tenant-one",
                () -> controller.readFile("one.txt", null, null, exchange("/api/workspace/file")));
        Map<String, Object> tenantTwoFile = withTenant("tenant-two",
                () -> controller.readFile("two.txt", null, null, exchange("/api/workspace/file")));

        assertEquals("one", tenantOneFile.get("content"));
        assertEquals("two", tenantTwoFile.get("content"));

        ResponseStatusException notFound = org.junit.jupiter.api.Assertions.assertThrows(ResponseStatusException.class,
                () -> withTenant("tenant-two",
                        () -> controller.readFile("one.txt", null, null, exchange("/api/workspace/file"))));
        assertEquals(HttpStatus.NOT_FOUND, notFound.getStatusCode());
    }

    private void openWorkspaceForTenant(WorkspaceController controller, String tenantId, Path workspacePath) {
        Map<String, Object> response = withTenant(tenantId,
                () -> controller.openWorkspace(Map.of("path", workspacePath.toString()), exchange("/api/workspace/open")));
        assertEquals(workspacePath.toString(), response.get("path"));
    }

    private ServerWebExchange exchange(String path) {
        return MockServerWebExchange.from(MockServerHttpRequest.get(path).build());
    }

    private void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private <T> T withTenant(String tenantId, TenantAction<T> action) {
        TenantContext.set(tenantId);
        try {
            return action.execute();
        } finally {
            TenantContext.clear();
        }
    }

    @FunctionalInterface
    private interface TenantAction<T> {
        T execute();
    }
}
