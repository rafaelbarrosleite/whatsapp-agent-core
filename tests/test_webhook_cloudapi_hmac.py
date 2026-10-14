"""Testes de integração — validação HMAC no /webhook/cloudapi.

Usa um payload que CloudApiAdapter.eh_mensagem_entrada() descarta
("object" != "whatsapp_business_account") para exercitar só a camada de
validação HMAC sem precisar mockar TenantResolver nem Supabase.

Os 4 casos obrigatórios:
  (a) assinatura válida     → 200
  (b) assinatura inválida   → 401
  (c) header ausente        → 401
  (d) body adulterado com assinatura antiga → 401 (replay/tamper)
"""

import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_SECRET = "test-app-secret"
_PAYLOAD = {"object": "page"}  # descartado pelo adapter — não precisa de tenant
_BODY: bytes = json.dumps(_PAYLOAD, separators=(",", ":")).encode()


def _assinar(body: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(body: bytes, headers: dict) -> object:
    return client.post(
        "/webhook/cloudapi",
        content=body,
        headers={"content-type": "application/json", **headers},
    )


# ─── (a) assinatura válida → 200 ─────────────────────────────────────────────


def test_assinatura_valida_aceita_request() -> None:
    sig = _assinar(_BODY)
    with patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET):
        resp = _post(_BODY, {"x-hub-signature-256": sig})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignorado"}


# ─── (b) assinatura inválida → 401 ───────────────────────────────────────────


def test_assinatura_invalida_retorna_401() -> None:
    sig_errada = _assinar(_BODY, secret="outro-secret")
    with patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET):
        resp = _post(_BODY, {"x-hub-signature-256": sig_errada})

    assert resp.status_code == 401


# ─── (c) header ausente → 401 ────────────────────────────────────────────────


def test_header_ausente_retorna_401() -> None:
    with patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET):
        resp = _post(_BODY, {})  # sem x-hub-signature-256

    assert resp.status_code == 401


# ─── (d) body adulterado com assinatura antiga → 401 ─────────────────────────


def test_body_adulterado_com_assinatura_original_retorna_401() -> None:
    """Replay/tamper: assinatura calculada sobre o body original, body modificado."""
    sig_original = _assinar(_BODY)
    body_adulterado = _BODY + b',"extra":1}'

    with patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET):
        resp = _post(body_adulterado, {"x-hub-signature-256": sig_original})

    assert resp.status_code == 401


# ─── Sem secret configurado — fail-closed → 500 ──────────────────────────────


def test_sem_secret_configurado_retorna_500() -> None:
    """Fail-closed: secret ausente é erro de configuração do servidor, não do cliente."""
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", None),
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret_secundario", None),
    ):
        resp = _post(_BODY, {})

    assert resp.status_code == 500


# ─── Migração entre apps da Meta: dois App Secrets ─────────────────────────

_SECRET_SECUNDARIO = "secret-app-secundario"


def test_assinatura_do_app_secundario_aceita() -> None:
    sig = _assinar(_BODY, secret=_SECRET_SECUNDARIO)
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch(
            "gateway.webhook_cloudapi.settings.whatsapp_app_secret_secundario", _SECRET_SECUNDARIO
        ),
    ):
        resp = _post(_BODY, {"x-hub-signature-256": sig})

    assert resp.status_code == 200


def test_assinatura_do_app_antigo_continua_aceita_com_os_dois() -> None:
    sig = _assinar(_BODY)
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch(
            "gateway.webhook_cloudapi.settings.whatsapp_app_secret_secundario", _SECRET_SECUNDARIO
        ),
    ):
        resp = _post(_BODY, {"x-hub-signature-256": sig})

    assert resp.status_code == 200


def test_assinatura_de_outro_app_rejeitada_com_os_dois() -> None:
    sig = _assinar(_BODY, secret="terceiro-app")
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch(
            "gateway.webhook_cloudapi.settings.whatsapp_app_secret_secundario", _SECRET_SECUNDARIO
        ),
    ):
        resp = _post(_BODY, {"x-hub-signature-256": sig})

    assert resp.status_code == 401


def test_so_o_secret_secundario_configurado_basta() -> None:
    """Depois de desligar o app antigo, WHATSAPP_APP_SECRET pode sair."""
    sig = _assinar(_BODY, secret=_SECRET_SECUNDARIO)
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", None),
        patch(
            "gateway.webhook_cloudapi.settings.whatsapp_app_secret_secundario", _SECRET_SECUNDARIO
        ),
    ):
        resp = _post(_BODY, {"x-hub-signature-256": sig})

    assert resp.status_code == 200
