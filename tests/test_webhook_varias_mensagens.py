"""A Meta pode mandar várias mensagens (e vários tipos de evento) num POST só.
Antes só entry[0].changes[0].messages[0] era lido e o resto se perdia."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from channels.base import TipoMensagem
from channels.cloud_api import CloudApiAdapter
from gateway.tenant_resolver import TenantInfo

_SECRET = "test-app-secret"
_adapter = CloudApiAdapter()
_TENANT = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")


def _texto(mid: str, de: str = "5567988880000", body: str = "oi") -> dict:
    return {
        "from": de,
        "id": mid,
        "timestamp": "1719792000",
        "type": "text",
        "text": {"body": body},
    }


def _value(*messages: dict, contacts: list | None = None) -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5511888880000", "phone_number_id": "PHONE-1"},
        "contacts": contacts or [],
        "messages": list(messages),
    }


def _payload(*changes: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [c]} for c in changes],
    }


def _change(value: dict, field: str = "messages") -> dict:
    return {"field": field, "value": value}


def _post(payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    assinatura = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return TestClient(app).post(
        "/webhook/cloudapi",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": assinatura},
    )


def _ctx(claim=None, lote=None, eco=None):
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    return (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", claim or MagicMock(return_value=True)),
        patch("gateway.webhook_cloudapi.criar_sender", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", lote or AsyncMock()),
        patch("gateway.webhook_cloudapi.processar_eco_celular", eco or MagicMock()),
    )


# ─── Adapter ─────────────────────────────────────────────────────────────────


def test_adapter_le_todas_as_mensagens_de_todas_as_entradas() -> None:
    payload = _payload(
        _change(_value(_texto("m1"), _texto("m2"))),
        _change(_value(_texto("m3", de="5567977770000"))),
    )
    msgs = _adapter.normalizar_todas(payload)
    assert [m.message_id for m in msgs] == ["m1", "m2", "m3"]
    assert msgs[2].numero_remetente == "+5567977770000"


def test_adapter_descarta_so_a_mensagem_malformada() -> None:
    payload = _payload(_change(_value({"type": "text"}, _texto("ok"))))
    assert [m.message_id for m in _adapter.normalizar_todas(payload)] == ["ok"]


def test_adapter_casa_o_nome_pelo_wa_id() -> None:
    contatos = [
        {"wa_id": "5567911110000", "profile": {"name": "Ana"}},
        {"wa_id": "5567922220000", "profile": {"name": "Bruno"}},
    ]
    payload = _payload(
        _change(
            _value(
                _texto("a", de="5567911110000"), _texto("b", de="5567922220000"), contacts=contatos
            )
        )
    )
    assert [m.nome_remetente for m in _adapter.normalizar_todas(payload)] == ["Ana", "Bruno"]


def test_adapter_ignora_status_e_outros_fields() -> None:
    status = _change({"metadata": {}, "statuses": [{"id": "x", "status": "read"}]})
    assert _adapter.eh_mensagem_entrada(_payload(status)) is False
    eco = _change(_value(_texto("m1")), field="smb_message_echoes")
    assert _adapter.eh_mensagem_entrada(_payload(eco)) is False
    assert _adapter.normalizar_todas(_payload(eco)) == []


def test_adapter_mensagem_no_segundo_change_e_detectada() -> None:
    status = _change({"metadata": {}, "statuses": [{"id": "x"}]})
    payload = _payload(status, _change(_value(_texto("m1"))))
    assert _adapter.eh_mensagem_entrada(payload) is True
    assert _adapter.normalizar(payload).message_id == "m1"


# ─── Webhook ─────────────────────────────────────────────────────────────────


def test_webhook_registra_e_processa_todas_as_mensagens() -> None:
    claim = MagicMock(return_value=True)
    lote = AsyncMock()
    payload = _payload(_change(_value(_texto("m1"), _texto("m2"))))
    p = _ctx(claim=claim, lote=lote)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        resp = _post(payload)

    assert resp.status_code == 200
    assert [c.kwargs["event_id"] for c in claim.call_args_list] == ["m1", "m2"]
    # Mesma conversa → um lote só com as duas, agente roda uma vez.
    lote.assert_awaited_once()
    assert [i.event_id for i in lote.await_args.args[0]] == ["m1", "m2"]


def test_webhook_pacientes_diferentes_no_mesmo_post_viram_lotes_separados() -> None:
    lote = AsyncMock()
    payload = _payload(_change(_value(_texto("a", de="5567911110000"), _texto("b"))))
    p = _ctx(lote=lote)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        _post(payload)
    assert sorted(c.args[0][0].event_id for c in lote.await_args_list) == ["a", "b"]


def test_webhook_falha_de_registro_numa_pede_retry_mas_processa_as_outras() -> None:
    def _claim(*_a, event_id, **_k):
        if event_id == "m2":
            raise RuntimeError("db")
        return True

    lote = AsyncMock()
    payload = _payload(_change(_value(_texto("m1"), _texto("m2"))))
    p = _ctx(claim=MagicMock(side_effect=_claim), lote=lote)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        resp = _post(payload)

    assert resp.status_code == 503  # a Meta reenvia; m1 volta como duplicada
    assert [i.event_id for i in lote.await_args.args[0]] == ["m1"]


def test_webhook_todas_duplicadas_responde_duplicate() -> None:
    lote = AsyncMock()
    p = _ctx(claim=MagicMock(return_value=False), lote=lote)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        resp = _post(_payload(_change(_value(_texto("m1"), _texto("m2")))))
    assert resp.json() == {"status": "ok", "duplicate": True}
    lote.assert_not_awaited()


def test_webhook_eco_e_mensagem_no_mesmo_post_tratam_os_dois() -> None:
    eco_payload = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5511888880000", "phone_number_id": "PHONE-1"},
        "message_echoes": [
            {
                "from": "5511888880000",
                "to": "5567911110000",
                "id": "eco1",
                "timestamp": "1719792000",
                "type": "text",
                "text": {"body": "oi"},
            }
        ],
    }
    lote = AsyncMock()
    eco = MagicMock()
    payload = _payload(
        _change(eco_payload, field="smb_message_echoes"), _change(_value(_texto("m1")))
    )
    p = _ctx(lote=lote, eco=eco)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        resp = _post(payload)
    assert resp.status_code == 200
    eco.assert_called_once()
    lote.assert_awaited_once()


def test_webhook_rajada_ponta_a_ponta_responde_uma_vez() -> None:
    """Duas mensagens de texto do mesmo paciente no mesmo POST: o agente roda
    uma vez com as duas, e as duas ficam gravadas no histórico."""
    from langchain_core.messages import AIMessage

    db = MagicMock()
    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    grafo = MagicMock()
    grafo.aget_state = AsyncMock(return_value=MagicMock(values={"x": 1}))
    grafo.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="Claro!")], "tenant_config": {}}
    )
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    payload = _payload(_change(_value(_texto("m1", body="oi"), _texto("m2", body="quero marcar"))))
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=db),
        patch("gateway.webhook_cloudapi.claim_event", return_value=True),
        patch("gateway.webhook_cloudapi.criar_sender", return_value=sender),
        patch("gateway.idempotent_processing.get_supabase", return_value=db),
        patch("gateway.idempotent_processing.finish_event") as finish,
        patch("core.pipeline.get_supabase", return_value=db),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conv-1", True, "reuso_aberta"),
        ),
        patch("core.pipeline.handle_consentimento", new=AsyncMock(return_value=None)),
        patch("core.pipeline.handle_resposta_lembrete", new=AsyncMock(return_value=None)),
        patch("core.pipeline.handle_resposta_nps", new=AsyncMock(return_value=None)),
        patch("core.pipeline._grafo", grafo),
        patch("core.pipeline.salvar_mensagem_db") as salvar,
    ):
        resp = _post(payload)

    assert resp.status_code == 200
    grafo.ainvoke.assert_awaited_once()
    assert grafo.ainvoke.await_args.args[0]["messages"][0].content == "oi\nquero marcar"
    sender.enviar_texto.assert_awaited_once()
    assert [(c.args[3], c.args[4]) for c in salvar.call_args_list[:2]] == [
        ("paciente", "oi"),
        ("paciente", "quero marcar"),
    ]
    assert [c.args[3] for c in salvar.call_args_list[2:]] == ["agente"]
    assert sorted(c.kwargs["event_id"] for c in finish.call_args_list) == ["m1", "m2"]


def test_tipo_texto_continua_mapeado() -> None:
    m = _adapter.normalizar(_payload(_change(_value(_texto("m1")))))
    assert m.tipo == TipoMensagem.texto
