import json
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.observability import JsonFormatter, bind_tenant, correlation_id_var, metrics


def test_health_returns_correlation_id() -> None:
    response = TestClient(app).get("/health", headers={"x-correlation-id": "corr-123"})
    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "corr-123"


def test_metrics_exposes_operational_counters_without_tenant_data() -> None:
    metrics.increment("test_counter")
    with patch("app.main.settings.internal_api_key", "chave-interna"):
        response = TestClient(app).get(
            "/metrics", headers={"Authorization": "Bearer chave-interna"}
        )
    assert response.status_code == 200
    assert response.json()["test_counter"] >= 1


@pytest.mark.parametrize(
    ("configurada", "header"),
    [
        (None, None),  # sem chave configurada: falha fechado
        (None, "Bearer qualquer"),
        ("chave-interna", None),
        ("chave-interna", "Bearer errada"),
        ("chave-interna", "chave-interna"),  # sem o prefixo Bearer
    ],
)
def test_metrics_exige_chave_interna(configurada, header) -> None:
    headers = {"Authorization": header} if header else {}
    with patch("app.main.settings.internal_api_key", configurada):
        response = TestClient(app).get("/metrics", headers=headers)
    assert response.status_code == 401


def test_structured_log_contains_correlation_and_tenant() -> None:
    correlation_id_var.set("corr-log")
    bind_tenant("tenant-log")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "processed", (), None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["correlation_id"] == "corr-log"
    assert payload["tenant_id"] == "tenant-log"
    assert payload["message"] == "processed"
