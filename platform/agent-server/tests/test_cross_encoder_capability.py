from app.rag.reranking.cross_encoder import cross_encoder_available


def test_cross_encoder_capability_probe_is_boolean():
    assert isinstance(cross_encoder_available(), bool)
