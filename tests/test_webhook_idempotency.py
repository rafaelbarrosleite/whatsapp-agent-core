import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from gateway.tenant_resolver import TenantInfo

client = TestClient(app)
secret = "test-app-secret"


def _payload(message_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "5511888880000",
                                "phone_number_id": "PHONE-1",
                            },
                            "contacts": [{"profile": {"name": "Paciente"}}],
                            "messages": [
                                {
                                    "from": "5567988880000",
                                    "id": message_id,
                                    "timestamp": "1719792000",
                                    "type": "text",
                                    "text": {"body": "Olá"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def _post(payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/cloudapi",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": signature},
    )


def _patches(claimed: bool):
    resolver = MagicMock()
    resolver.resolver.return_value = TenantInfo(
        "tenant-1", "clinica", "canal-1", "cloud_api", "ref"
    )
    return (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", secret),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", return_value=claimed),
        patch("gateway.webhook_cloudapi.criar_sender", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", new=AsyncMock()),
    )


def test_repeated_webhook_is_acknowledged_without_second_processing() -> None:
    patches = _patches(False)
    with patches[0], patches[1], patches[2], patches[3] as claim, patches[4], patches[5] as process:
        response = _post(_payload())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "duplicate": True}
    claim.assert_called_once()
    process.assert_not_called()


def test_idempotency_storage_failure_requests_provider_retry() -> None:
    patches = _patches(True)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("gateway.webhook_cloudapi.claim_event", side_effect=RuntimeError("db down")),
        patches[4],
        patches[5],
    ):
        response = _post(_payload("wamid.retry"))
    assert response.status_code == 503


def test_canal_sem_credencial_nao_processa_nem_registra_evento() -> None:
    """Item 1.8: sem credencial própria do canal, a mensagem não é processada
    (nem respondida pelo número global) e o webhook confirma o recebimento."""
    from channels.credenciais import CredencialCanalAusenteError

    resolver = MagicMock()
    resolver.resolver.return_value = TenantInfo(
        "tenant-1", "clinica", "canal-1", "cloud_api", "SEM_ENV"
    )
    claim = MagicMock(return_value=True)
    processar = MagicMock()
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", secret),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", claim),
        patch(
            "gateway.webhook_cloudapi.criar_sender",
            side_effect=CredencialCanalAusenteError("SEM_ENV"),
        ),
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", new=processar),
    ):
        response = _post(_payload("wamid.sem-credencial"))

    assert response.status_code == 200
    claim.assert_not_called()
    processar.assert_not_called()
