"""Coexistência: histórico (`history`) e agenda (`smb_app_state_sync`) do app
WhatsApp Business, mandados pela Meta quando a clínica conecta o número."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from channels.base import ContatoAgenda, ConversaHistorico, MensagemHistorico
from channels.cloud_api import CloudApiAdapter
from db import historico
from gateway import historico_coexistencia
from gateway.tenant_resolver import TenantInfo

_SECRET = "test-app-secret"
_adapter = CloudApiAdapter()
_TENANT = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")
_CLINICA = "5567977776666"
_PACIENTE = "5567988880000"


def _payload(field: str, value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": field, "value": value}]}],
    }


def _historico(*threads: dict, errors: list | None = None) -> dict:
    bloco: dict = {"metadata": {"phase": 0, "chunk_order": 1, "progress": 100}}
    if threads:
        bloco["threads"] = list(threads)
    if errors:
        bloco = {"errors": errors}
    return _payload(
        "history",
        {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": _CLINICA, "phone_number_id": "PHONE-1"},
            "history": [bloco],
        },
    )


def _thread() -> dict:
    return {
        "id": _PACIENTE,
        "messages": [
            {
                "from": _PACIENTE,
                "to": _CLINICA,
                "id": "h1",
                "timestamp": "1719792000",
                "type": "text",
                "text": {"body": "Tem horário sexta?"},
                "history_context": {"status": "READ"},
            },
            {
                "from": _CLINICA,
                "to": _PACIENTE,
                "id": "h2",
                "timestamp": "1719792060",
                "type": "image",
                "image": {"caption": "Nosso endereço"},
            },
            {"from": _PACIENTE, "id": "h3", "timestamp": "1719792120", "type": "audio"},
            {"id": "sem-from", "type": "text", "text": {"body": "x"}},
        ],
    }


def _agenda(*itens: dict) -> dict:
    return _payload(
        "smb_app_state_sync",
        {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": _CLINICA, "phone_number_id": "PHONE-1"},
            "state_sync": list(itens),
        },
    )


def _contato(acao: str = "add", nome: str | None = "Maria Souza") -> dict:
    contato: dict = {"phone_number": _PACIENTE}
    if nome:
        contato |= {"full_name": nome, "first_name": nome.split()[0]}
    return {"type": "contact", "contact": contato, "action": acao, "metadata": {"timestamp": "1"}}


# ─── Adapter ─────────────────────────────────────────────────────────────────


def test_adapter_le_o_historico_de_quem_escreveu() -> None:
    conversas, erros = _adapter.normalizar_historico(_historico(_thread()))
    assert erros == []
    assert len(conversas) == 1
    c = conversas[0]
    assert (c.numero_negocio, c.numero_cliente) == ("+5567977776666", "+5567988880000")
    assert [(m.message_id, m.do_paciente, m.texto) for m in c.mensagens] == [
        ("h1", True, "Tem horário sexta?"),
        ("h2", False, "Nosso endereço"),
        ("h3", True, "[áudio]"),
    ]


def test_adapter_reporta_historico_recusado() -> None:
    erro = {"code": 2593109, "title": "History sync is turned off"}
    conversas, erros = _adapter.normalizar_historico(_historico(errors=[erro]))
    assert conversas == []
    assert erros == ["2593109: History sync is turned off"]


def test_adapter_le_a_agenda() -> None:
    contatos = _adapter.normalizar_agenda(_agenda(_contato(), _contato("remove", None)))
    assert [(c.numero_cliente, c.nome, c.acao) for c in contatos] == [
        ("+5567988880000", "Maria Souza", "add"),
        ("+5567988880000", None, "remove"),
    ]


def test_sincronizacao_nao_e_mensagem_de_paciente() -> None:
    p = _historico(_thread())
    assert _adapter.eh_sincronizacao_app(p) is True
    assert _adapter.eh_mensagem_entrada(p) is False
    assert _adapter.eh_eco_celular(p) is False


# ─── Banco ───────────────────────────────────────────────────────────────────


def _conversa_hist() -> ConversaHistorico:
    def m(mid: str, paciente: bool, texto: str, ts: int) -> MensagemHistorico:
        return MensagemHistorico(
            message_id=mid,
            do_paciente=paciente,
            tipo="text",
            texto=texto,
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        )

    return ConversaHistorico(
        numero_negocio="+5567977776666",
        numero_cliente="+5567988880000",
        mensagens=[m("h2", False, "Temos sim", 200), m("h1", True, "Tem horário?", 100)],
    )


def _client(contato=None, conversa_hist=None, ja_importados=()):
    """Client fake: cada tabela responde ao select com os dados dados."""
    client = MagicMock()
    tabelas: dict[str, MagicMock] = {}

    def table(nome):
        if nome not in tabelas:
            t = MagicMock()
            q = t.select.return_value
            for metodo in ("eq", "in_", "limit"):
                getattr(q, metodo).return_value = q
            if nome == "contatos":
                q.execute.return_value.data = [contato] if contato else []
                t.insert.return_value.execute.return_value.data = [{"id": "contato-novo"}]
            elif nome == "conversas":
                q.execute.return_value.data = [{"id": conversa_hist}] if conversa_hist else []
                t.insert.return_value.execute.return_value.data = [{"id": "conv-hist"}]
            elif nome == "mensagens":
                q.execute.return_value.data = [{"whatsapp_message_id": i} for i in ja_importados]
            tabelas[nome] = t
        return tabelas[nome]

    for nome in ("contatos", "conversas", "mensagens"):
        table(nome)
    client.table.side_effect = table
    return client, tabelas


def test_importa_paciente_novo_em_conversa_encerrada_sem_consentimento() -> None:
    client, t = _client()
    n = historico.importar_conversa_historico(client, "tenant-1", "canal-1", _conversa_hist())

    assert n == 2
    contato = t["contatos"].insert.call_args.args[0]
    assert contato["lgpd_aceito"] is False
    assert contato["telefone"] == "+5567988880000"
    conversa = t["conversas"].insert.call_args.args[0]
    assert conversa["estado"] == "encerrada"
    assert conversa["agente_ativo"] is False
    assert conversa["metadata"] == {"origem": "historico_whatsapp"}
    linhas = t["mensagens"].insert.call_args.args[0]
    # Ordem cronológica; clínica com o mesmo rótulo do eco do celular.
    assert [(r["papel"], r["conteudo"]) for r in linhas] == [
        ("usuario", "Tem horário?"),
        ("agente", "Clínica (celular): Temos sim"),
    ]
    assert linhas[0]["metadata"]["whatsapp_message_id"] == "h1"
    assert linhas[0]["created_at"].startswith("1970-01-01T00:01:40")


def test_reimportar_nao_duplica() -> None:
    client, t = _client(
        contato={"id": "c-1", "lgpd_revogado": False, "anonimizado": False},
        conversa_hist="conv-hist",
        ja_importados=("h1",),
    )
    n = historico.importar_conversa_historico(client, "tenant-1", "canal-1", _conversa_hist())
    assert n == 1
    t["contatos"].insert.assert_not_called()
    t["conversas"].insert.assert_not_called()
    assert [
        r["metadata"]["whatsapp_message_id"] for r in t["mensagens"].insert.call_args.args[0]
    ] == ["h2"]


def test_paciente_que_revogou_consentimento_nao_tem_historico_importado() -> None:
    client, t = _client(contato={"id": "c-1", "lgpd_revogado": True, "anonimizado": False})
    assert historico.importar_conversa_historico(client, "t", "c", _conversa_hist()) == 0
    t["mensagens"].insert.assert_not_called()


def test_agenda_preenche_nome_vazio_de_contato_existente() -> None:
    client, t = _client(contato={"id": "c-1", "nome": None})
    contato = ContatoAgenda(
        numero_negocio="+1", numero_cliente="+5567988880000", nome="Maria", acao="add"
    )
    assert historico.aplicar_contato_agenda(client, "tenant-1", contato) is True
    t["contatos"].update.assert_called_once_with({"nome": "Maria"})


@pytest.mark.parametrize(
    ("existente", "acao", "nome"),
    [
        ({"id": "c-1", "nome": "Já tem"}, "add", "Maria"),  # nunca sobrescreve
        (None, "add", "Maria"),  # nunca cria contato pela agenda
        ({"id": "c-1", "nome": None}, "remove", None),  # nunca apaga
    ],
)
def test_agenda_nao_sobrescreve_nao_cria_nao_apaga(existente, acao, nome) -> None:
    client, t = _client(contato=existente)
    contato = ContatoAgenda(numero_negocio="+1", numero_cliente="+55679", nome=nome, acao=acao)
    assert historico.aplicar_contato_agenda(client, "tenant-1", contato) is False
    t["contatos"].update.assert_not_called()
    t["contatos"].insert.assert_not_called()


# ─── Processamento e webhook ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_processar_importa_e_conclui_evento() -> None:
    with (
        patch.object(historico_coexistencia, "get_supabase", return_value=MagicMock()),
        patch.object(
            historico_coexistencia, "importar_conversa_historico", return_value=2
        ) as importar,
        patch.object(historico_coexistencia, "aplicar_contato_agenda", return_value=True),
        patch.object(historico_coexistencia, "finish_event") as finish,
    ):
        await historico_coexistencia.processar_sincronizacao_app(
            tenant=_TENANT,
            conversas=[_conversa_hist()],
            contatos=[],
            event_id="ev-1",
        )
    importar.assert_called_once()
    assert finish.call_args.kwargs["success"] is True
    assert finish.call_args.kwargs["provider"] == "cloud_api_historico"


@pytest.mark.asyncio
async def test_falha_num_paciente_nao_para_os_outros() -> None:
    with (
        patch.object(historico_coexistencia, "get_supabase", return_value=MagicMock()),
        patch.object(
            historico_coexistencia,
            "importar_conversa_historico",
            side_effect=[RuntimeError("x"), 3],
        ) as importar,
        patch.object(historico_coexistencia, "finish_event") as finish,
    ):
        await historico_coexistencia.processar_sincronizacao_app(
            tenant=_TENANT,
            conversas=[_conversa_hist(), _conversa_hist()],
            contatos=[],
            event_id="ev-1",
        )
    assert importar.call_count == 2
    assert finish.call_args.kwargs["success"] is False


def _post(payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    assinatura = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return TestClient(app).post(
        "/webhook/cloudapi",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": assinatura},
    )


def test_webhook_agenda_o_import_do_historico_sem_acionar_a_bia() -> None:
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    claim = MagicMock(return_value=True)
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", claim),
        patch("gateway.webhook_cloudapi.processar_sincronizacao_app", new=AsyncMock()) as proc,
        patch("gateway.webhook_cloudapi.processar_lote_idempotente", new=AsyncMock()) as agente,
    ):
        resp = _post(_historico(_thread()))

    assert resp.status_code == 200
    resolver.resolver.assert_called_with("+5567977776666")
    assert claim.call_args.kwargs["provider"] == "cloud_api_historico"
    proc.assert_awaited_once()
    assert len(proc.await_args.kwargs["conversas"]) == 1
    agente.assert_not_awaited()


def test_webhook_historico_duplicado_nao_reimporta() -> None:
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    with (
        patch("gateway.webhook_cloudapi.settings.whatsapp_app_secret", _SECRET),
        patch("gateway.webhook_cloudapi._get_resolver", return_value=resolver),
        patch("gateway.webhook_cloudapi.get_supabase", return_value=MagicMock()),
        patch("gateway.webhook_cloudapi.claim_event", return_value=False),
        patch("gateway.webhook_cloudapi.processar_sincronizacao_app", new=AsyncMock()) as proc,
    ):
        resp = _post(_agenda(_contato()))
    assert resp.status_code == 200
    proc.assert_not_awaited()


# ─── Endpoint que pede a sincronização à Meta ────────────────────────────────


def test_sincronizar_exige_chave_interna() -> None:
    with patch("app.main.settings.internal_api_key", "chave"):
        r = TestClient(app).post(
            "/admin/coexistencia/sincronizar",
            json={"numero": "+5567977776666", "tipo": "history"},
            headers={"Authorization": "Bearer errada"},
        )
    assert r.status_code == 401


def test_sincronizar_valida_tipo() -> None:
    with patch("app.main.settings.internal_api_key", "chave"):
        r = TestClient(app).post(
            "/admin/coexistencia/sincronizar",
            json={"numero": "+5567977776666", "tipo": "tudo"},
            headers={"Authorization": "Bearer chave"},
        )
    assert r.status_code == 400


def test_sincronizar_pede_a_meta_pelo_canal_do_numero() -> None:
    resolver = MagicMock()
    resolver.resolver.return_value = _TENANT
    sender = MagicMock()
    sender.solicitar_sincronizacao_app = AsyncMock(return_value=(True, '{"success":true}'))
    with (
        patch("app.main.settings.internal_api_key", "chave"),
        patch("gateway.tenant_resolver.TenantResolver", return_value=resolver),
        patch("channels.sender_factory.criar_sender", return_value=sender),
    ):
        r = TestClient(app).post(
            "/admin/coexistencia/sincronizar",
            json={"numero": "+5567977776666", "tipo": "history"},
            headers={"Authorization": "Bearer chave"},
        )
    assert r.status_code == 200
    assert r.json()["aceito"] is True
    sender.solicitar_sincronizacao_app.assert_awaited_once_with("history")


@pytest.mark.asyncio
async def test_sender_chama_smb_app_data() -> None:
    from channels.cloud_api_sender import CloudApiSender

    resposta = MagicMock(status_code=200, text='{"success":true}')
    cliente = MagicMock()
    cliente.post = AsyncMock(return_value=resposta)
    cliente.__aenter__ = AsyncMock(return_value=cliente)
    cliente.__aexit__ = AsyncMock(return_value=False)
    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=cliente):
        aceito, _ = await CloudApiSender("PN-1", "tok").solicitar_sincronizacao_app(
            "smb_app_state_sync"
        )
    assert aceito is True
    assert cliente.post.await_args.args[0].endswith("/PN-1/smb_app_data")
    assert cliente.post.await_args.kwargs["json"] == {
        "messaging_product": "whatsapp",
        "sync_type": "smb_app_state_sync",
    }
