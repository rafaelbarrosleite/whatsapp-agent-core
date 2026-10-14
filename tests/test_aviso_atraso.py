"""POST /api/recepcao/aviso-atraso (itens 4.1 e 1.8).

O telefone sai do banco pelo contato_id, filtrado pela clínica; o envio vai
pelo canal da própria clínica; sem canal utilizável, 503 e nada enviado."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.atendente_router import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

_HEADERS = {"Authorization": "Bearer chave-secreta-teste"}


def _settings() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = "chave-secreta-teste"
    return s


def _vertical(perfil: dict | None) -> MagicMock:
    vertical = MagicMock()
    query = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.in_.return_value.limit.return_value.execute.return_value.data = [perfil] if perfil else []
    return vertical


def _core(contatos: list[dict], config: dict | None = None) -> MagicMock:
    core = MagicMock()
    tabelas = {"contatos": MagicMock(), "tenants": MagicMock()}
    contatos_q = tabelas["contatos"].select.return_value.eq.return_value.in_.return_value
    contatos_q.execute.return_value.data = contatos
    tenants_q = tabelas["tenants"].select.return_value.eq.return_value.limit.return_value
    tenants_q.execute.return_value.data = [{"config": config or {}}]
    core.table.side_effect = lambda nome: tabelas[nome]
    core.tabelas = tabelas
    return core


def _body(**over) -> dict:
    base = {
        "tenant_id": "tenant-1",
        "perfil_id": "perfil-1",
        "avisos": [
            {"contato_id": "c1", "texto": "Olá, Ana! Atraso de 20 min."},
            {"contato_id": "c-outra-clinica", "texto": "Olá! Atraso de 20 min."},
        ],
    }
    base.update(over)
    return base


def _chamar(
    body: dict, *, perfil: dict | None, contatos: list[dict], sender, config: dict | None = None
) -> tuple:
    core = _core(contatos, config)
    with (
        patch("app.atendente_router.settings", _settings()),
        patch("app.atendente_router.get_vertical_supabase", return_value=_vertical(perfil)),
        patch("app.atendente_router.get_supabase", return_value=core),
        patch("app.atendente_router.sender_do_tenant", return_value=sender) as sdt,
    ):
        resp = client.post("/api/recepcao/aviso-atraso", json=body, headers=_HEADERS)
    return resp, core, sdt


_PERFIL = {"id": "perfil-1", "nome": "Carla", "role": "atendente", "tenant_id": "tenant-1"}


def test_sem_auth_401() -> None:
    with patch("app.atendente_router.settings", _settings()):
        assert client.post("/api/recepcao/aviso-atraso", json=_body()).status_code == 401


def test_envia_so_para_contatos_da_clinica_pelo_canal_dela() -> None:
    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    resp, core, sdt = _chamar(
        _body(), perfil=_PERFIL, contatos=[{"id": "c1", "telefone": "+5567911"}], sender=sender
    )

    assert resp.status_code == 200
    assert resp.json() == {"enviados": 1, "falhas": 1}
    sender.enviar_texto.assert_awaited_once_with("+5567911", "Olá, Ana! Atraso de 20 min.")
    core.tabelas["contatos"].select.return_value.eq.assert_called_with("tenant_id", "tenant-1")
    assert sdt.call_args.args[0] == "tenant-1"


def test_perfil_de_outra_clinica_404() -> None:
    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    resp, _, _ = _chamar(
        _body(), perfil=None, contatos=[{"id": "c1", "telefone": "+5567911"}], sender=sender
    )
    assert resp.status_code == 404
    sender.enviar_texto.assert_not_called()


def test_canal_sem_credencial_503_e_nada_enviado() -> None:
    resp, _, _ = _chamar(
        _body(), perfil=_PERFIL, contatos=[{"id": "c1", "telefone": "+5567911"}], sender=None
    )
    assert resp.status_code == 503
    assert "canal WhatsApp" in resp.json()["detail"]


def test_limite_de_50_avisos_e_texto_obrigatorio() -> None:
    sender = MagicMock()
    muitos = [{"contato_id": f"c{i}", "texto": "x"} for i in range(51)]
    assert (
        _chamar(_body(avisos=muitos), perfil=_PERFIL, contatos=[], sender=sender)[0].status_code
        == 422
    )
    vazio = [{"contato_id": "c1", "texto": ""}]
    assert (
        _chamar(_body(avisos=vazio), perfil=_PERFIL, contatos=[], sender=sender)[0].status_code
        == 422
    )


def test_com_template_aviso_atraso_envia_template() -> None:
    sender = MagicMock()
    sender.enviar_template = AsyncMock(return_value=True)
    sender.enviar_texto = AsyncMock(return_value=True)
    body = _body(
        avisos=[
            {
                "contato_id": "c1",
                "texto": "Olá, Ana! Atraso de 20 min.",
                "parametros": ["Ana", "Clínica\nOrto", "20", "10:30"],
            }
        ]
    )
    config = {"templates": {"aviso_atraso": {"nome": "aviso_atraso"}}}
    resp, _, _ = _chamar(
        body,
        perfil=_PERFIL,
        contatos=[{"id": "c1", "telefone": "+5567911"}],
        sender=sender,
        config=config,
    )
    assert resp.json() == {"enviados": 1, "falhas": 0}
    sender.enviar_template.assert_awaited_once_with(
        "+5567911", "aviso_atraso", "pt_BR", ["Ana", "Clínica Orto", "20", "10:30"]
    )
    sender.enviar_texto.assert_not_called()


def test_sem_template_usa_o_texto_mesmo_com_parametros() -> None:
    sender = MagicMock()
    sender.enviar_template = AsyncMock(return_value=True)
    sender.enviar_texto = AsyncMock(return_value=True)
    body = _body(
        avisos=[{"contato_id": "c1", "texto": "Olá!", "parametros": ["Ana", "20", "10:30", "C"]}]
    )
    _chamar(body, perfil=_PERFIL, contatos=[{"id": "c1", "telefone": "+5567911"}], sender=sender)
    sender.enviar_template.assert_not_called()
    sender.enviar_texto.assert_awaited_once_with("+5567911", "Olá!")
