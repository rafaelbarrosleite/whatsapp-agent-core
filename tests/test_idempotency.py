from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.base import MensagemRecebida, ProviderWhatsApp, TipoMensagem
from db.idempotency import claim_event, external_event_id, finish_event, payload_sha256
from gateway.idempotent_processing import processar_evento_idempotente
from gateway.tenant_resolver import TenantInfo


def _rpc_client(data=True) -> MagicMock:
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = data
    return client


def test_external_event_id_uses_provider_id_when_present() -> None:
    assert external_event_id(" wamid.123 ", b"body") == "wamid.123"


def test_external_event_id_falls_back_to_stable_payload_hash() -> None:
    raw = b'{"same":"payload"}'
    assert external_event_id("", raw) == f"sha256:{payload_sha256(raw)}"
    assert external_event_id(None, raw) == external_event_id("", raw)


def test_claim_event_passes_tenant_hash_and_external_id_to_atomic_rpc() -> None:
    client = _rpc_client(True)
    assert (
        claim_event(
            client,
            provider="cloud_api",
            event_id="wamid.1",
            tenant_id="tenant-1",
            raw_body=b"payload",
        )
        is True
    )
    client.rpc.assert_called_once_with(
        "claim_idempotency_event",
        {
            "p_provider": "cloud_api",
            "p_external_event_id": "wamid.1",
            "p_tenant_id": "tenant-1",
            "p_payload_hash": payload_sha256(b"payload"),
            "p_stale_after_seconds": 300,
        },
    )


def test_claim_event_returns_false_for_concurrent_duplicate() -> None:
    assert (
        claim_event(
            _rpc_client(False),
            provider="cloud_api",
            event_id="msg-1",
            tenant_id="tenant-1",
            raw_body=b"payload",
        )
        is False
    )


def test_finish_event_persists_failure_for_safe_retry() -> None:
    client = _rpc_client()
    finish_event(
        client,
        provider="cloud_api",
        event_id="wamid.1",
        success=False,
        error="timeout",
    )
    params = client.rpc.call_args.args[1]
    assert params["p_success"] is False
    assert params["p_error"] == "timeout"


@pytest.mark.asyncio
async def test_processing_marks_unhandled_failure_as_retryable() -> None:
    message = MensagemRecebida(
        message_id="msg-1",
        numero_destino="+5511888880000",
        numero_remetente="+5567988880000",
        tipo=TipoMensagem.texto,
        texto="oi",
        provider=ProviderWhatsApp.cloud_api,
        timestamp="2026-08-03T00:00:00Z",
    )
    tenant = TenantInfo("tenant-1", "clinica", "canal-1")
    client = MagicMock()

    with (
        patch("gateway.idempotent_processing.get_supabase", return_value=client),
        patch(
            "gateway.idempotent_processing.processar_mensagem_entrada",
            new=AsyncMock(side_effect=RuntimeError("timeout")),
        ),
        patch("gateway.idempotent_processing.finish_event") as finish,
    ):
        await processar_evento_idempotente(
            provider="cloud_api",
            event_id="msg-1",
            mensagem=message,
            tenant=tenant,
            sender=MagicMock(),
        )

    finish.assert_called_once_with(
        client,
        provider="cloud_api",
        event_id="msg-1",
        success=False,
        error="timeout",
    )
