package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import com.example.agent.session.Message;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.regex.Pattern;

/**
 * Heuristic intent and topic classification helpers.
 */
@Component
public class IntentClassifier {

    private static final List<Pattern> META_PATTERNS = List.of(
            Pattern.compile("what (model|llm|ai) (are you|do you) us"),
            Pattern.compile("which (model|llm|provider)"),
            Pattern.compile("do you support (agent|agentic|autonomous)"),
            Pattern.compile("what (tools|capabilities) do you have"),
            Pattern.compile("how (do you|does this) work"),
            Pattern.compile("what is your (name|version)"),
            Pattern.compile("(help|usage|commands)"),
            Pattern.compile("(who|what) are you")
    );

    private static final List<Pattern> TOPIC_SWITCH_PATTERNS = List.of(
            Pattern.compile("^(now|next|also|btw|by the way|another|different|new|switch)"),
            Pattern.compile("^(can you|could you|please) (?!continue|keep|finish|complete)"),
            Pattern.compile("^(forget|ignore|stop|cancel|never ?mind)")
    );

    private static final List<Pattern> COMPLEX_PATTERNS = List.of(
            Pattern.compile("(fix|debug|refactor|redesign|architect|migrate|implement|build)"),
            Pattern.compile("(across|multiple|all) (files|modules|services|components)"),
            Pattern.compile("(why|how) .{50,}"),
            Pattern.compile("(test|spec|coverage|benchmark|performance)"),
            Pattern.compile("(deploy|ci|cd|docker|kubernetes)")
    );

    private static final List<Pattern> SIMPLE_PATTERNS = List.of(
            Pattern.compile("^(what|where|which|show|list|find|read|cat|grep) "),
            Pattern.compile("^(explain|summarize|describe) .{0,50}$"),
            Pattern.compile("(status|version|config|health)")
    );

    private final AgentProperties properties;

    public IntentClassifier() {
        this(new AgentProperties());
    }

    public IntentClassifier(AgentProperties properties) {
        this.properties = properties;
    }

    /**
     * Checks whether the request is about the assistant itself.
     */
    public boolean isMetaQuestion(String text) {
        String lower = normalize(text);
        return META_PATTERNS.stream().anyMatch(pattern -> pattern.matcher(lower).find());
    }

    /**
     * Returns a direct answer for common meta questions.
     */
    public String getMetaAnswer(String text) {
        String lower = normalize(text);
        if (Pattern.compile("what (model|llm)").matcher(lower).find()) {
            return "I'm using **" + properties.defaultModel() + "** as the default model.";
        }
        if (Pattern.compile("do you support (agent|agentic|autonomous)").matcher(lower).find()) {
            return "Yes — this API supports agent-style orchestration, streaming, and tool event hooks.";
        }
        if (Pattern.compile("what (tools|capabilities)").matcher(lower).find()) {
            return "I support chat completions, workspace browsing, config loading, and hybrid RAG search.";
        }
        if (Pattern.compile("(who|what) are you").matcher(lower).find()) {
            return "I'm the Java agent server for coding workflows, exposing an OpenAI-compatible API.";
        }
        return "";
    }

    /**
     * Detects whether the latest user message likely starts a new topic.
     */
    public boolean detectTopicSwitch(String currentMessage, List<Message> previousMessages) {
        if (previousMessages == null || previousMessages.isEmpty()) {
            return false;
        }
        String lower = normalize(currentMessage);
        if (lower.split("\\s+").length <= 8) {
            return TOPIC_SWITCH_PATTERNS.stream().anyMatch(pattern -> pattern.matcher(lower).find());
        }
        return false;
    }

    /**
     * Classifies the request as simple or complex.
     */
    public String classifyComplexity(String text) {
        String lower = normalize(text);
        long complexScore = COMPLEX_PATTERNS.stream().filter(pattern -> pattern.matcher(lower).find()).count();
        long simpleScore = SIMPLE_PATTERNS.stream().filter(pattern -> pattern.matcher(lower).find()).count();
        return complexScore > simpleScore ? "complex" : "simple";
    }

    private String normalize(String text) {
        return text == null ? "" : text.toLowerCase().trim();
    }
}
