"""Safe homologation path: signed Meta webhook through the real local pipeline.

All external dependencies are isolated. This proves orchestration and persistence
contracts, not provider delivery or production configuration.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.main import app
from gateway.tenant_resolver import TenantInfo


def _payload() -> dict:
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
                            "contacts": [{"profile": {"name": "Paciente Teste"}}],
                            "messages": [
                                {
                                    "from": "5567988880000",
                                    "id": "wamid.homologation-1",
                                    "timestamp": "1719792000",
                                    "type": "text",
                                    "text": {"body": "Quero agendar"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def test_signed_webhook_runs_agent_persists_history_and_finishes_claim() -> None:
    secret = "homologation-secret"
    body = json.dumps(_payload(), separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    tenant = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")
    resolver = MagicMock()
    resolver.resolver.return_value = tenant
    db = MagicMock()
    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=MagicMock(values={}))
    graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Claro, vou ajudar com o agendamento.")],
            "contato_id": "contact-1",
            "tenant_config": {},
        }
    )

    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", secret),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=db),
        patch("gateway.webhook_cloudapi.claim_event", return_value=True) as claim,
        patch("gateway.webhook_cloudapi.criar_sender", return_value=sender),
        patch("gateway.idempotent_processing.get_supabase", return_value=db),
        patch("gateway.idempotent_processing.finish_event") as finish,
        patch("core.pipeline.get_supabase", return_value=db),
        patch("core.pipeline.resolver_contato_id", return_value="contact-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversation-1", True, "reuso_aberta"),
        ),
        patch("core.pipeline.handle_consentimento", new=AsyncMock(return_value=None)),
        patch("core.pipeline.handle_resposta_lembrete", new=AsyncMock(return_value=None)),
        patch("core.pipeline.handle_resposta_nps", new=AsyncMock(return_value=None)),
        patch("core.pipeline._grafo", graph),
        patch("core.pipeline.salvar_mensagem_db") as save_message,
    ):
        response = TestClient(app).post(
            "/webhook/cloudapi",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": signature,
                "x-correlation-id": "homologation-correlation",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "homologation-correlation"
    claim.assert_called_once()
    graph.ainvoke.assert_awaited_once()
    sender.enviar_texto.assert_awaited_once()
    assert save_message.call_count == 2
    assert save_message.call_args_list[0].args[3] == "paciente"
    assert save_message.call_args_list[1].args[3] == "agente"
    finish.assert_called_once_with(
        db,
        provider="cloud_api",
        event_id="wamid.homologation-1",
        success=True,
    )
