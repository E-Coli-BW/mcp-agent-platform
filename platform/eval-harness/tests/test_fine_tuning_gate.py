from ci.fine_tuning_gate import decide


def test_gate_defers_until_feedback_is_large_enough():
    result = decide({"feedback_samples": 20, "quality_rate": 0.4, "held_out_set": True})
    assert result["decision"] == "defer_insufficient_data"


def test_gate_requires_held_out_set():
    result = decide({"feedback_samples": 600, "quality_rate": 0.4, "held_out_set": False})
    assert result["decision"] == "defer_no_held_out_set"


def test_gate_allows_candidate_training_only_when_needed():
    result = decide({"feedback_samples": 600, "quality_rate": 0.4, "held_out_set": True})
    assert result["decision"] == "train_candidate_then_gate"
