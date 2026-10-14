from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.atendente_router import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

_HEADERS = {"Authorization": "Bearer chave-secreta-teste"}

_PATCH_SETTINGS = "app.atendente_router.settings"
_PATCH_VERTICAL_DB = "app.atendente_router.get_vertical_supabase"
_PATCH_CORE_DB = "app.atendente_router.get_supabase"
_PATCH_ENVIAR = "app.atendente_router.enviar_mensagem_tenant"
_PATCH_BUSCAR_HANDOFF_ATIVO = "app.atendente_router.buscar_handoff_ativo"
_PATCH_ASSUMIR_HANDOFF = "app.atendente_router.assumir_handoff"


def _mock_settings():
    settings = MagicMock()
    settings.internal_api_key = "chave-secreta-teste"
    return settings


def _mock_vertical_com_perfil(perfil_id="perfil-1", nome="Maria") -> MagicMock:
    vertical = MagicMock()
    chain = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.limit.return_value.execute.return_value.data = [
        {"id": perfil_id, "nome": nome, "tenant_id": "tenant-1"}
    ]
    return vertical


def _mock_core_db(
    agente_ativo: bool,
    telefone: str | None = "+5511999990000",
    atribuido_para: str | None = "perfil-1",
) -> MagicMock:
    core = MagicMock()
    tabelas: dict[str, MagicMock] = {}

    def table_side_effect(nome):
        if nome not in tabelas:
            t = MagicMock()
            chain = t.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute
            if nome == "conversas":
                chain.return_value.data = [
                    {
                        "id": "conversa-1",
                        "tenant_id": "tenant-1",
                        "contato_id": "contato-1",
                        "agente_ativo": agente_ativo,
                        "atribuido_para": atribuido_para,
                    }
                ]
            elif nome == "contatos":
                chain.return_value.data = [{"telefone": telefone}] if telefone else []
            tabelas[nome] = t
        return tabelas[nome]

    core.table.side_effect = table_side_effect
    return core


def _mock_busca_perfil_com_roles(vertical: MagicMock, perfil: dict) -> None:
    query = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.in_.return_value.limit.return_value.execute.return_value.data = [perfil]


def _body(**overrides):
    base = {"perfil_id": "perfil-1", "texto": "Olá, posso ajudar?", "tenant_id": "tenant-1"}
    base.update(overrides)
    return base


def test_sem_authorization_retorna_401():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.post("/api/conversas/conversa-1/mensagem-atendente", json=_body())

    assert resposta.status_code == 401


def test_authorization_errada_retorna_401():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers={"Authorization": "Bearer chave-errada"},
        )

    assert resposta.status_code == 401


def test_perfil_de_outro_tenant_retorna_404():
    vertical = MagicMock()
    chain = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.limit.return_value.execute.return_value.data = []

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_VERTICAL_DB, return_value=vertical):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 404


def test_agente_ativo_true_retorna_400():
    """Não pode permitir atendente responder enquanto o agente ainda está
    respondendo essa conversa automaticamente."""
    vertical = _mock_vertical_com_perfil()
    core = _mock_core_db(agente_ativo=True)

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 400


def test_sucesso_envia_prefixado_e_persiste():
    vertical = _mock_vertical_com_perfil(nome="Maria")
    core = _mock_core_db(agente_ativo=False, telefone="+5511999990000")

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)) as mock_enviar,
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch("app.atendente_router.salvar_mensagem") as mock_salvar,
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(texto="Pode vir na quinta às 14h"),
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    payload = resposta.json()
    assert payload["sucesso"] is True
    assert payload["mensagem_enviada"] == "Maria: Pode vir na quinta às 14h"

    mock_enviar.assert_called_once()
    assert mock_enviar.call_args.kwargs["numero_destino"] == "+5511999990000"
    assert mock_enviar.call_args.kwargs["texto"] == "Maria: Pode vir na quinta às 14h"

    mock_salvar.assert_called_once()
    assert mock_salvar.call_args.args[3] == "atendente"
    assert mock_salvar.call_args.args[4] == "Maria: Pode vir na quinta às 14h"
    assert mock_salvar.call_args.kwargs["perfil_id"] == "perfil-1"


def test_falha_no_envio_whatsapp_retorna_502_e_nao_persiste():
    vertical = _mock_vertical_com_perfil()
    core = _mock_core_db(agente_ativo=False)

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=False)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch("app.atendente_router.salvar_mensagem") as mock_salvar,
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 502
    mock_salvar.assert_not_called()


def test_mensagem_atendente_bloqueia_quem_nao_assumiu():
    """BUG 1: só quem assumiu (atribuido_para) pode responder. Outro perfil —
    inclusive admin — recebe 403 e a mensagem NÃO é enviada."""
    vertical = _mock_vertical_com_perfil(perfil_id="perfil-1", nome="Maria")
    core = _mock_core_db(agente_ativo=False, atribuido_para="outro-perfil")

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)) as mock_enviar,
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 403
    mock_enviar.assert_not_called()


# ─── /pausar ─────────────────────────────────────────────────────────────────


def test_pausar_sem_authorization_retorna_401():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.post(
            "/api/conversas/conversa-1/pausar",
            json={"tenant_id": "tenant-1", "perfil_id": "perfil-1"},
        )
    assert resposta.status_code == 401


def test_pausar_perfil_inexistente_retorna_404():
    vertical = MagicMock()
    chain = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.limit.return_value.execute.return_value.data = []

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_VERTICAL_DB, return_value=vertical):
        resposta = client.post(
            "/api/conversas/conversa-1/pausar",
            json={"tenant_id": "tenant-1", "perfil_id": "perfil-1"},
            headers=_HEADERS,
        )
    assert resposta.status_code == 404


def test_pausar_sucesso_seta_agente_ativo_false_e_atribui():
    vertical = _mock_vertical_com_perfil(perfil_id="admin-1", nome="Patrícia")
    core = _mock_core_db(agente_ativo=True)

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/pausar",
            json={"tenant_id": "tenant-1", "perfil_id": "admin-1"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    assert resposta.json() == {"sucesso": True}

    update_calls = [c for c in core.table("conversas").update.call_args_list]
    assert any(c.args[0].get("agente_ativo") is False for c in update_calls)
    assert any(c.args[0].get("atribuido_nome") == "Patrícia" for c in update_calls)


# ─── /atribuir ───────────────────────────────────────────────────────────────


def test_atribuir_recusa_se_agente_ainda_ativo():
    vertical = _mock_vertical_com_perfil(perfil_id="atendente-1", nome="Carlos")
    core = _mock_core_db(agente_ativo=True)

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/atribuir",
            json={"tenant_id": "tenant-1", "perfil_id": "atendente-1"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 400


def test_atribuir_sucesso_atualiza_conversa_e_handoff():
    vertical = _mock_vertical_com_perfil(perfil_id="atendente-1", nome="Carlos")
    _mock_busca_perfil_com_roles(
        vertical,
        {"id": "atendente-1", "nome": "Carlos", "role": "atendente", "tenant_id": "tenant-1"},
    )
    core = _mock_core_db(agente_ativo=False)

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/atribuir",
            json={"tenant_id": "tenant-1", "perfil_id": "atendente-1"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    assert resposta.json() == {"sucesso": True}
    vertical.table.return_value.update.assert_called()


# ─── /assumir ───────────────────────────────────────────────────────────────


def test_assumir_sucesso_pausa_agente_e_atribui():
    """Novo fluxo: no assumir a conversa ainda está com o agente ativo; o
    assumir é quem pausa (agente_ativo=False) e atribui o lead a quem assumiu."""
    vertical = _mock_vertical_com_perfil(perfil_id="atendente-1", nome="Carlos")
    _mock_busca_perfil_com_roles(
        vertical,
        {"id": "atendente-1", "nome": "Carlos", "role": "atendente", "tenant_id": "tenant-1"},
    )
    core = _mock_core_db(agente_ativo=True, atribuido_para=None)
    handoff = {"id": "handoff-1", "status": "aguardando"}

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=handoff)),
        patch("app.atendente_router.atribuir_lead_por_contato", new=AsyncMock()) as mock_atribuir,
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/assumir",
            json={"tenant_id": "tenant-1", "perfil_id": "atendente-1"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    assert resposta.json() == {"sucesso": True, "status": "em_atendimento"}
    update_calls = [c.args[0] for c in core.table("conversas").update.call_args_list]
    assert any(payload.get("agente_ativo") is False for payload in update_calls)
    mock_atribuir.assert_awaited_once()


def test_assumir_handoff_ja_em_atendimento_retorna_409():
    """Anti-duplo-assumir: se o handoff já foi assumido (em_atendimento), um
    segundo atendente recebe 409 em vez de assumir por cima."""
    vertical = _mock_vertical_com_perfil(perfil_id="atendente-2", nome="Ana")
    _mock_busca_perfil_com_roles(
        vertical,
        {"id": "atendente-2", "nome": "Ana", "role": "atendente", "tenant_id": "tenant-1"},
    )
    core = _mock_core_db(agente_ativo=True, atribuido_para="atendente-1")
    handoff = {"id": "handoff-1", "status": "em_atendimento"}

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=handoff)),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/assumir",
            json={"tenant_id": "tenant-1", "perfil_id": "atendente-2"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 409


# ─── /devolver ───────────────────────────────────────────────────────────────


def test_devolver_conversa_inexistente_retorna_404():
    core = MagicMock()
    chain = core.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.limit.return_value.execute.return_value.data = []

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_CORE_DB, return_value=core):
        resposta = client.post(
            "/api/conversas/conversa-1/devolver",
            json={"tenant_id": "tenant-1"},
            headers=_HEADERS,
        )
    assert resposta.status_code == 404


def test_devolver_sucesso_chama_devolver_para_agente():
    core = _mock_core_db(agente_ativo=False)
    vertical = MagicMock()

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch("app.atendente_router.devolver_para_agente", new=AsyncMock()) as mock_devolver,
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/devolver",
            json={"tenant_id": "tenant-1"},
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    assert resposta.json() == {"sucesso": True}
    mock_devolver.assert_called_once_with("conversa-1", "tenant-1", core, vertical)


# ─── mensagem-atendente assume o handoff na primeira resposta ────────────────


def test_mensagem_atendente_assume_handoff_aguardando():
    vertical = _mock_vertical_com_perfil(nome="Maria")
    core = _mock_core_db(agente_ativo=False, telefone="+5511999990000")
    handoff = {"id": "handoff-1", "status": "aguardando"}

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=handoff)),
        patch(_PATCH_ASSUMIR_HANDOFF, new=AsyncMock()) as mock_assumir,
        patch("app.atendente_router.salvar_mensagem"),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    mock_assumir.assert_called_once_with("handoff-1", "tenant-1", vertical)


def test_mensagem_atendente_nao_assume_handoff_ja_em_atendimento():
    """Segunda mensagem em diante: handoff já está em_atendimento — não deve
    chamar assumir_handoff novamente."""
    vertical = _mock_vertical_com_perfil(nome="Maria")
    core = _mock_core_db(agente_ativo=False, telefone="+5511999990000")
    handoff = {"id": "handoff-1", "status": "em_atendimento"}

    with (
        patch(_PATCH_SETTINGS, _mock_settings()),
        patch(_PATCH_VERTICAL_DB, return_value=vertical),
        patch(_PATCH_CORE_DB, return_value=core),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=handoff)),
        patch(_PATCH_ASSUMIR_HANDOFF, new=AsyncMock()) as mock_assumir,
        patch("app.atendente_router.salvar_mensagem"),
    ):
        resposta = client.post(
            "/api/conversas/conversa-1/mensagem-atendente",
            json=_body(),
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    mock_assumir.assert_not_called()
