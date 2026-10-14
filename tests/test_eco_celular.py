"""Coexistência: a clínica responde pelo app WhatsApp Business no celular
(webhook smb_message_echoes) → a mensagem entra no histórico e o agente pausa."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import db.conversas as dbc
from app.main import app
from channels.cloud_api import CloudApiAdapter
from gateway.tenant_resolver import TenantInfo

_SECRET = "test-app-secret"
_adapter = CloudApiAdapter()


def _payload_eco(*ecos: dict, field: str = "smb_message_echoes") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-1",
                "changes": [
                    {
                        "field": field,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "5567977776666",
                                "phone_number_id": "PHONE-1",
                            },
                            "message_echoes": list(ecos),
                        },
                    }
                ],
            }
        ],
    }


def _eco_texto(message_id: str = "wamid.eco1", texto: str = "Oi, aqui é a clínica") -> dict:
    return {
        "from": "5567977776666",
        "to": "5567988880000",
        "id": message_id,
        "timestamp": "1719792000",
        "type": "text",
        "text": {"body": texto},
    }


# ─── Adapter ─────────────────────────────────────────────────────────────────


def test_reconhece_eco_do_celular() -> None:
    assert _adapter.eh_eco_celular(_payload_eco(_eco_texto())) is True
    assert _adapter.eh_mensagem_entrada(_payload_eco(_eco_texto())) is False


def test_outros_campos_nao_sao_eco() -> None:
    assert _adapter.eh_eco_celular(_payload_eco(_eco_texto(), field="history")) is False
    assert _adapter.eh_eco_celular(_payload_eco()) is False
    assert _adapter.eh_eco_celular({"object": "page"}) is False


def test_normaliza_eco_de_texto() -> None:
    [eco] = _adapter.normalizar_ecos(_payload_eco(_eco_texto()))
    assert eco.message_id == "wamid.eco1"
    assert eco.numero_negocio == "+5567977776666"
    assert eco.numero_cliente == "+5567988880000"
    assert eco.tipo == "text"
    assert eco.texto == "Oi, aqui é a clínica"


def test_normaliza_midia_com_e_sem_legenda_e_descarta_malformado() -> None:
    imagem = {**_eco_texto("wamid.img"), "type": "image", "image": {"id": "M1"}}
    imagem.pop("text")
    documento = {**_eco_texto("wamid.doc"), "type": "document", "document": {"caption": "Exame"}}
    documento.pop("text")
    sem_id = _eco_texto()
    sem_id.pop("id")
    ecos = _adapter.normalizar_ecos(_payload_eco(imagem, documento, sem_id))
    assert [(e.message_id, e.texto) for e in ecos] == [
        ("wamid.img", "[imagem]"),
        ("wamid.doc", "Exame"),
    ]


# ─── Banco: registra a mensagem e pausa o agente ────────────────────────────────


def _registrar(conversa_ativa: dict | None, contato_id: str | None = "contato-1"):
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.return_value.data = [{"id": "novo"}]
    with (
        patch.object(dbc, "resolver_contato_id", return_value=contato_id),
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=conversa_ativa),
        patch.object(dbc, "obter_ou_criar_conversa", return_value="conv-nova") as criar,
        patch.object(dbc, "salvar_mensagem") as salvar,
    ):
        resultado = dbc.registrar_mensagem_celular_clinica(
            client, "t-1", "5567988880000", "Oi", "wamid.1", "canal-1"
        )
    return client, resultado, criar, salvar


def _pausou(client: MagicMock, conversa_id: str) -> bool:
    for chamada in client.table.return_value.update.call_args_list:
        if chamada.args[0] == {"estado": "aguardando_humano", "agente_ativo": False}:
            return True
    return False


def test_conversa_aberta_com_bia_e_pausada_e_mensagem_salva() -> None:
    client, (contato, conversa), criar, salvar = _registrar(
        {"id": "conv-1", "estado": "aberta", "agente_ativo": True}
    )
    assert (contato, conversa) == ("contato-1", "conv-1")
    criar.assert_not_called()
    assert _pausou(client, "conv-1")
    args, kwargs = salvar.call_args
    assert args[1:5] == ("t-1", "conv-1", "atendente", "Clínica (celular): Oi")
    assert kwargs["metadata"] == {"origem": "celular_clinica", "message_id": "wamid.1"}


def test_conversa_ja_com_humano_nao_e_atualizada_de_novo() -> None:
    client, _, _, salvar = _registrar(
        {"id": "conv-1", "estado": "aguardando_humano", "agente_ativo": False}
    )
    assert not _pausou(client, "conv-1")
    salvar.assert_called_once()


def test_sem_conversa_cria_e_ja_nasce_pausada() -> None:
    client, (_, conversa), criar, _ = _registrar(None)
    assert conversa == "conv-nova"
    criar.assert_called_once_with(client, "t-1", "contato-1", "canal-1")
    assert _pausou(client, "conv-nova")


def test_contato_novo_e_criado_sem_consentimento() -> None:
    client, (contato, _), _, _ = _registrar(None, contato_id=None)
    assert contato == "novo"
    client.table.return_value.insert.assert_any_call(
        {"tenant_id": "t-1", "telefone": "+5567988880000", "lgpd_aceito": False}
    )


# ─── Webhook ─────────────────────────────────────────────────────────────────

client = TestClient(app)


def _post(payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/cloudapi",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": sig},
    )


def _resolver() -> MagicMock:
    r = MagicMock()
    r.resolver.return_value = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")
    return r


def test_webhook_despacha_eco_e_nao_aciona_a_bia() -> None:
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=_resolver()),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", return_value=True) as claim,
        patch("gateway.webhook_cloudapi.processar_eco_celular", new=MagicMock()) as eco,
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", new=AsyncMock()) as agente,
    ):
        resp = _post(_payload_eco(_eco_texto()))
    assert resp.status_code == 200
    assert claim.call_args.kwargs["provider"] == "cloud_api_eco"
    assert claim.call_args.kwargs["event_id"] == "wamid.eco1"
    eco.assert_called_once()
    assert eco.call_args.kwargs["tenant"].tenant_id == "tenant-1"
    agente.assert_not_called()


def test_webhook_eco_duplicado_nao_processa_de_novo() -> None:
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=_resolver()),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", return_value=False),
        patch("gateway.webhook_cloudapi.processar_eco_celular", new=MagicMock()) as eco,
    ):
        resp = _post(_payload_eco(_eco_texto()))
    assert resp.status_code == 200
    eco.assert_not_called()


def test_processar_eco_registra_e_conclui_evento() -> None:
    import asyncio

    from gateway import eco_celular

    [eco] = _adapter.normalizar_ecos(_payload_eco(_eco_texto()))
    tenant = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")
    with (
        patch.object(eco_celular, "get_supabase", return_value=MagicMock()),
        patch.object(eco_celular, "get_vertical_supabase", return_value=MagicMock()),
        patch.object(
            eco_celular, "registrar_mensagem_celular_clinica", return_value=("c-1", "conv-1")
        ) as registrar,
        patch.object(eco_celular, "mover_lead_para_com_atendente") as mover,
        patch.object(eco_celular, "finish_event") as finish,
    ):
        asyncio.run(
            eco_celular.processar_eco_celular(eco=eco, tenant=tenant, event_id="wamid.eco1")
        )
    registrar.assert_called_once()
    mover.assert_called_once()
    assert finish.call_args.kwargs["success"] is True
