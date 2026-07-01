package com.example.agent.tools;

import com.example.agent.config.AgentProperties;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

@Configuration
public class GitTools {

    private final Path workspaceRoot;

    public GitTools(AgentProperties agentProperties) {
        this.workspaceRoot = Paths.get(agentProperties.workspace()).toAbsolutePath().normalize();
    }

    @Bean
    public FunctionToolCallback<NoInput, String> gitStatus() {
        return FunctionToolCallback.<NoInput, String>builder(
                "git_status",
                (NoInput input) -> gitStatus(input)
            )
            .description("Show git status in the workspace.")
            .inputType(NoInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<NoInput, String> gitDiff() {
        return FunctionToolCallback.<NoInput, String>builder(
                "git_diff",
                (NoInput input) -> gitDiff(input)
            )
            .description("Show the git diff for the workspace.")
            .inputType(NoInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<GitCommitInput, String> gitCommit() {
        return FunctionToolCallback.<GitCommitInput, String>builder(
                "git_commit",
                (GitCommitInput input) -> gitCommit(input)
            )
            .description("Stage all changes and create a git commit.")
            .inputType(GitCommitInput.class)
            .build();
    }

    private String gitStatus(NoInput ignored) {
        try {
            Path root = workspaceRoot();
            CommandResult result = runCommand(List.of("git", "status", "--short"), root, Duration.ofSeconds(10));
            if (result.timedOut()) {
                return "❌ git status failed: timed out after 10 seconds";
            }
            if (result.exitCode() != 0) {
                String stderr = result.stderr().trim();
                if (stderr.contains("not a git repository")) {
                    runCommand(List.of("git", "init"), root, Duration.ofSeconds(10));
                    return "📂 Initialized new git repository. No changes yet.";
                }
                return "❌ git error: " + stderr;
            }
            String output = result.stdout().trim();
            if (output.isEmpty()) {
                return "✅ Working tree clean — no uncommitted changes.";
            }
            return "📂 Changes:\n```\n" + output + "\n```";
        } catch (Exception ex) {
            return "❌ git status failed: " + ex.getMessage();
        }
    }

    private String gitDiff(NoInput ignored) {
        try {
            Path root = workspaceRoot();
            CommandResult summary = runCommand(List.of("git", "diff", "--stat"), root, Duration.ofSeconds(10));
            if (summary.timedOut()) {
                return "❌ git diff failed: timed out after 10 seconds";
            }
            if (summary.stdout().trim().isEmpty()) {
                summary = runCommand(List.of("git", "diff", "--cached", "--stat"), root, Duration.ofSeconds(10));
            }
            String output = summary.stdout().trim();
            if (output.isEmpty()) {
                return "No changes to show.";
            }

            CommandResult fullDiff = runCommand(List.of("git", "diff"), root, Duration.ofSeconds(10));
            String diffText = fullDiff.stdout();
            if (diffText.isBlank()) {
                fullDiff = runCommand(List.of("git", "diff", "--cached"), root, Duration.ofSeconds(10));
                diffText = fullDiff.stdout();
            }
            if (diffText.length() > 2000) {
                diffText = diffText.substring(0, 2000) + "\n... (truncated)";
            }
            return "📊 Diff summary:\n" + output + "\n\n```diff\n" + diffText + "\n```";
        } catch (Exception ex) {
            return "❌ git diff failed: " + ex.getMessage();
        }
    }

    private String gitCommit(GitCommitInput input) {
        try {
            Path root = workspaceRoot();
            runCommand(List.of("git", "add", "-A"), root, Duration.ofSeconds(10));
            CommandResult result = runCommand(List.of("git", "commit", "-m", input.message()), root, Duration.ofSeconds(10));
            if (result.timedOut()) {
                return "❌ git commit failed: timed out after 10 seconds";
            }
            String stdout = result.stdout().trim();
            String stderr = result.stderr().trim();
            if (result.exitCode() == 0) {
                return "✅ Committed: " + input.message() + "\n" + stdout;
            }
            if (stdout.contains("nothing to commit") || stderr.contains("nothing to commit")) {
                return "ℹ️ Nothing to commit — working tree clean.";
            }
            return "❌ Commit failed: " + (stderr.isEmpty() ? stdout : stderr);
        } catch (Exception ex) {
            return "❌ git commit failed: " + ex.getMessage();
        }
    }

    private Path workspaceRoot() throws IOException {
        Files.createDirectories(workspaceRoot);
        return workspaceRoot;
    }

    private CommandResult runCommand(List<String> command, Path cwd, Duration timeout) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(command)
            .directory(cwd.toFile())
            .start();

        CompletableFuture<String> stdout = CompletableFuture.supplyAsync(() -> readStream(process.getInputStream()));
        CompletableFuture<String> stderr = CompletableFuture.supplyAsync(() -> readStream(process.getErrorStream()));
        boolean finished = process.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS);
        if (!finished) {
            process.destroyForcibly();
            return new CommandResult(-1, stdout.getNow(""), stderr.getNow(""), true);
        }
        return new CommandResult(process.exitValue(), stdout.join(), stderr.join(), false);
    }

    private String readStream(InputStream stream) {
        try (stream) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException ex) {
            return "";
        }
    }

    private record CommandResult(int exitCode, String stdout, String stderr, boolean timedOut) {}

    public record NoInput() {}

    public record GitCommitInput(String message) {}
}
