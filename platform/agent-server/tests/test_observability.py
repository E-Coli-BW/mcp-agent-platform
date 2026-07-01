"""Tests for observability decision logging."""

from app.observability.decision_log import (
    AgentMetrics,
    CompressionEvent,
    DecisionLogger,
    EventType,
    FactExtractionEvent,
    SkillActivationEvent,
    StateSnapshot,
    TokenBudgetEvent,
    get_agent_metrics,
    get_decision_logger,
)


class TestDecisionLogger:
    def test_logs_compression_event(self):
        logger = DecisionLogger()
        event = CompressionEvent(
            session_id="s1",
            turn_number=22,
            trigger="token_budget_exceeded",
            before_tokens=5000,
            after_tokens=2000,
            messages_dropped=8,
            messages_summarized=12,
            facts_retained=["root cause: race condition"],
        )
        logger.log_compression(event)

        buffer = logger.get_buffer()
        assert len(buffer) == 1
        assert buffer[0]["event_type"] == "compression"
        assert buffer[0]["data"]["compression_ratio"] == 0.4

    def test_logs_skill_activation(self):
        logger = DecisionLogger()
        event = SkillActivationEvent(
            session_id="s1",
            turn_number=8,
            layer=2,
            skill_key="maven-stale-jar-fix",
            match_score=1.5,
            match_reason="regex match on ClassNotFoundException",
        )
        logger.log_skill_activation(event)

        buffer = logger.get_buffer()
        assert buffer[0]["event_type"] == "skill_activation"
        assert buffer[0]["data"]["layer"] == 2

    def test_logs_fact_extraction(self):
        logger = DecisionLogger()
        event = FactExtractionEvent(
            session_id="s1",
            turn_number=12,
            new_facts=["root cause is in async handler"],
            hypothesis_changed=True,
        )
        logger.log_fact_extraction(event)
        assert len(logger.get_buffer()) == 1

    def test_logs_token_budget(self):
        logger = DecisionLogger()
        event = TokenBudgetEvent(
            session_id="s1",
            turn_number=5,
            total_tokens=3000,
            budget_tokens=5000,
        )
        logger.log_token_budget(event)
        assert logger.get_buffer()[0]["data"]["usage_pct"] == 0.6

    def test_logs_state_snapshot(self):
        logger = DecisionLogger()
        event = StateSnapshot(
            session_id="s1",
            turn_number=20,
            event="pre_compression",
            message_count=45,
            token_estimate=4800,
            investigation_summary="Debugging NPE in UserService",
        )
        logger.log_snapshot(event)
        assert logger.get_buffer()[0]["data"]["event"] == "pre_compression"

    def test_clear_buffer(self):
        logger = DecisionLogger()
        logger.log_compression(CompressionEvent(
            session_id="s1", turn_number=1, trigger="test",
            before_tokens=100, after_tokens=50,
            messages_dropped=0, messages_summarized=0,
        ))
        assert len(logger.get_buffer()) == 1
        logger.clear_buffer()
        assert len(logger.get_buffer()) == 0


class TestAgentMetrics:
    def test_record_compression(self):
        metrics = AgentMetrics()
        metrics.record_compression(0.5)
        assert metrics.compressions_total == 1

    def test_record_skill_activation_by_layer(self):
        metrics = AgentMetrics()
        metrics.record_skill_activation(2)
        metrics.record_skill_activation(2)
        metrics.record_skill_activation(3)
        assert metrics.skill_activations_total == 3
        assert metrics.skill_activations_by_layer[2] == 2
        assert metrics.skill_activations_by_layer[3] == 1

    def test_to_dict(self):
        metrics = AgentMetrics()
        metrics.record_fact_extraction(5)
        d = metrics.to_dict()
        assert d["facts_extracted_total"] == 5
        assert "compressions_total" in d


class TestSingletons:
    def test_get_decision_logger_returns_same_instance(self):
        a = get_decision_logger()
        b = get_decision_logger()
        assert a is b

    def test_get_agent_metrics_returns_same_instance(self):
        a = get_agent_metrics()
        b = get_agent_metrics()
        assert a is b
