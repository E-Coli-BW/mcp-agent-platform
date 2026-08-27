from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.middleware import AuthContext, require_auth
from app.main import app


@pytest.fixture
def client():
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


def _authenticate(*permissions: str) -> None:
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        user_id="api-test",
        tenant_id="tenant-a",
        permissions=list(permissions),
    )


def test_sensitive_api_routes_require_authentication(client):
    feedback = client.post("/api/feedback", json={"rating": "positive"})
    retrain = client.post("/api/reranker/retrain")
    metrics = client.get("/metrics")

    assert feedback.status_code == 401
    assert retrain.status_code == 401
    assert metrics.status_code == 401


def test_authenticated_user_can_record_tenant_scoped_feedback(client, monkeypatch):
    captured = {}

    def record(**fields):
        captured.update(fields)
        return {"feedback_id": "feedback-test", **fields}

    monkeypatch.setattr("app.feedback.store.record_user_feedback", record)
    _authenticate("CHAT")

    response = client.post(
        "/api/feedback",
        json={"rating": "positive", "answer": "helpful"},
    )

    assert response.status_code == 200
    assert captured["user_id"] == "api-test"
    assert captured["tenant_id"] == "tenant-a"


def test_regular_user_cannot_mutate_global_reranker(client):
    _authenticate("CHAT")

    response = client.post("/api/reranker/retrain")

    assert response.status_code == 403


def test_regular_user_cannot_read_global_metrics(client):
    _authenticate("CHAT")

    response = client.get("/metrics")

    assert response.status_code == 403


def test_super_admin_can_read_global_metrics(client, monkeypatch):
    monkeypatch.setattr(
        "app.middleware.tracing.get_metrics_summary",
        lambda: {"requests": {}, "latency": {}},
    )
    _authenticate("*")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.json() == {"requests": {}, "latency": {}}


def test_super_admin_can_retrain_reranker(client, monkeypatch):
    reranker = MagicMock()
    reranker.retrain.return_value = {"version": "candidate-test"}
    reranker.get_weights_info.return_value = {"active_version": "v1"}
    monkeypatch.setattr(
        "app.rag.reranking.learned.get_learned_reranker",
        lambda: reranker,
    )
    _authenticate("*")

    response = client.post("/api/reranker/retrain")

    assert response.status_code == 200
    assert response.json()["status"] == "candidate_created"
    reranker.retrain.assert_called_once_with(min_samples=5, promote=False)