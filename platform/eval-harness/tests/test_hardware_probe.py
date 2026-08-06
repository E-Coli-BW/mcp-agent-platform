from ci.hardware_probe import probe


def test_hardware_probe_is_safe_and_structured():
    result = probe()
    assert result["machine"]
    assert isinstance(result["ollama_available"], bool)
    assert isinstance(result["cuda_available"], bool)
