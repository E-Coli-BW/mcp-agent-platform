package com.example.agent;

import com.example.agent.agent.IntentClassifier;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class IntentClassifierTest {

    private final IntentClassifier classifier = new IntentClassifier();

    @Test
    void should_detectMetaQuestion_when_askingAboutModel() {
        assertTrue(classifier.isMetaQuestion("what model are you using?"));
        assertTrue(classifier.isMetaQuestion("which LLM provider"));
    }

    @Test
    void should_notDetectMeta_when_codingQuestion() {
        assertFalse(classifier.isMetaQuestion("fix the null pointer exception"));
        assertFalse(classifier.isMetaQuestion("read the file src/main.py"));
    }

    @Test
    void should_returnSimple_when_shortQuestion() {
        assertEquals("simple", classifier.classifyComplexity("what is the main function?"));
        assertEquals("simple", classifier.classifyComplexity("show me the config"));
        assertEquals("simple", classifier.classifyComplexity("list all files in src"));
    }

    @Test
    void should_returnComplex_when_debugRequest() {
        assertEquals("complex", classifier.classifyComplexity("debug the authentication issue across multiple services"));
        assertEquals("complex", classifier.classifyComplexity("refactor the database layer"));
        assertEquals("complex", classifier.classifyComplexity("implement a new caching system with Redis"));
    }

    @Test
    void should_returnMetaAnswer_when_askingWhoAreYou() {
        String answer = classifier.getMetaAnswer("who are you?");
        assertNotNull(answer);
        assertFalse(answer.isEmpty());
    }
}
