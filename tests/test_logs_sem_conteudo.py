"""Garantia LGPD: o CONTEÚDO de mensagens (paciente, médico, IA) nunca vai ao log.

Cada teste injeta um texto-marcador único pelos caminhos reais (webhooks,
pipeline, transcrição de áudio, erro de tool, canal interno do médico) e
falha se o marcador aparecer em QUALQUER registro de log — mensagem
formatada ou traceback. Logs devem carregar só ids, tamanhos e tipos.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from app.main import app
from channels.base import MensagemRecebida, TipoMensagem
from gateway.tenant_resolver import TenantInfo

MARCADOR = "DOR-NO-PEITO-MARCADOR-7f3a"
RESPOSTA_IA = "RESPOSTA-DA-IA-MARCADOR-91bc"

_TENANT = TenantInfo(
    tenant_id="t-1",
    vertical="clinica",
    canal_id="canal-1",
    provider="cloud_api",
    credenciais_ref=None,
)

client = TestClient(app)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _sem_marcador(caplog: pytest.LogCaptureFixture, *marcadores: str) -> None:
    # caplog.text inclui mensagens formatadas e tracebacks (exc_info).
    texto = caplog.text
    for record in caplog.records:
        texto += record.getMessage()
    for marcador in marcadores:
        assert marcador not in texto, f"conteúdo vazou para o log: {marcador!r}"


def _mensagem(texto: str, tipo: TipoMensagem = TipoMensagem.texto) -> MensagemRecebida:
    return MensagemRecebida(
        message_id="msg-1",
        numero_destino="+556733334444",
        numero_remetente="+5567988880000",
        nome_remetente="Maria",
        tipo=tipo,
        texto=texto,
        provider="cloud_api",
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )


# ─── Webhooks ────────────────────────────────────────────────────────────────


def test_webhook_cloudapi_nao_loga_texto(logs) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "5511888880000",
                                "phone_number_id": "P1",
                            },
                            "contacts": [{"profile": {"name": "Maria"}}],
                            "messages": [
                                {
                                    "from": "5567988880000",
                                    "id": "C1",
                                    "timestamp": "1719792000",
                                    "type": "text",
                                    "text": {"body": MARCADOR},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    assinatura = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", "sec"),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.criar_sender", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", return_value=True),
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/webhook/cloudapi",
            content=body,
            headers={"content-type": "application/json", "x-hub-signature-256": assinatura},
        )

    assert resp.status_code == 200
    assert "Mensagem recebida" in logs.text
    _sem_marcador(logs, MARCADOR)


# ─── Pipeline ────────────────────────────────────────────────────────────────


def _patches_pipeline(grafo):
    return (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-1", True, "reuso_aberta"),
        ),
        patch("core.pipeline.handle_consentimento", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_lembrete", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_nps", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.obter_ou_criar_conversa", return_value="conversa-1"),
        patch("core.pipeline.salvar_mensagem_db"),
        patch("core.pipeline._grafo", grafo),
    )


def _grafo_respondendo(texto: str) -> MagicMock:
    grafo = MagicMock()
    grafo.aget_state = AsyncMock(return_value=MagicMock(values={"ja": True}))
    grafo.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content=texto)], "tenant_config": {}}
    )
    return grafo


async def test_pipeline_nao_loga_texto_do_paciente_nem_resposta(logs) -> None:
    from core.pipeline import processar_mensagem_entrada

    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    p = _patches_pipeline(_grafo_respondendo(RESPOSTA_IA))
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8]:
        await processar_mensagem_entrada(_mensagem(MARCADOR), _TENANT, sender, "cloud_api")

    assert "Resposta gerada" in logs.text
    _sem_marcador(logs, MARCADOR, RESPOSTA_IA)


async def test_pipeline_nao_loga_transcricao_de_audio(logs) -> None:
    from core.pipeline import processar_mensagem_entrada

    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    mensagem = _mensagem("", tipo=TipoMensagem.audio).model_copy(
        update={"texto": None, "metadata": {"media_id": "m1", "mime_type": "audio/ogg"}}
    )
    p = _patches_pipeline(_grafo_respondendo(RESPOSTA_IA))
    with (
        p[0],
        p[1],
        p[2],
        p[3],
        p[4],
        p[5],
        p[6],
        p[7],
        p[8],
        patch("core.pipeline.settings.groq_api_key", "g"),
        patch(
            "core.pipeline.baixar_midia_meta",
            new_callable=AsyncMock,
            return_value=(b"x", "audio/ogg"),
        ),
        patch("core.pipeline.transcrever_audio", new_callable=AsyncMock, return_value=MARCADOR),
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    assert "Áudio transcrito" in logs.text
    _sem_marcador(logs, MARCADOR, RESPOSTA_IA)


# ─── Erro de tool: os argumentos (resumo, nome…) não podem ir ao log ─────────


async def test_erro_de_tool_nao_loga_valores_dos_argumentos(logs) -> None:
    from core import nodes

    tool = MagicMock()
    tool.name = "transferir_para_atendente"
    tool.ainvoke = AsyncMock(side_effect=RuntimeError("falha interna"))
    ultima = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "transferir_para_atendente",
                "args": {"resumo": MARCADOR, "role_destino": "atendente"},
                "id": "call-1",
            }
        ],
    )
    state = {
        "messages": [HumanMessage(content="oi"), ultima],
        "tenant_id": "t-1",
        "vertical": "clinica",
        "tenant_config": {},
    }
    with patch("core.nodes._tools_disponiveis", return_value=[tool]):
        await nodes.executar_tools(state)

    assert "arg_keys" in logs.text
    assert "resumo" in logs.text  # o NOME do argumento pode aparecer
    _sem_marcador(logs, MARCADOR)


# ─── Canal interno do médico ─────────────────────────────────────────────────


async def test_agente_interno_nao_loga_resposta(logs) -> None:
    from app import webhook_interno

    with (
        patch.object(webhook_interno, "_grafo_interno", _grafo_respondendo(RESPOSTA_IA)),
        patch.object(
            webhook_interno, "enviar_mensagem_tenant", new_callable=AsyncMock, return_value=True
        ),
        patch.object(webhook_interno, "get_supabase", return_value=MagicMock()),
    ):
        await webhook_interno._processar_mensagem_medico(MARCADOR, {"id": "t-1", "config": {}})

    assert "Resposta gerada" in logs.text
    _sem_marcador(logs, MARCADOR, RESPOSTA_IA)
