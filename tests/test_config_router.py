from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config_router import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

_HEADERS = {"Authorization": "Bearer chave-secreta-teste"}

_PATCH_SETTINGS = "app.config_router.settings"
_PATCH_CORE_DB = "app.config_router.get_supabase"


def _mock_settings():
    settings = MagicMock()
    settings.internal_api_key = "chave-secreta-teste"
    return settings


def _mock_core_com_tenant(config: dict | None = None) -> MagicMock:
    core = MagicMock()
    chain = core.table.return_value.select.return_value.eq.return_value.limit.return_value.execute
    chain.return_value.data = [{"id": "tenant-1", "config": config or {}}]
    core.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        {"id": "tenant-1"}
    ]
    return core


def _body(**overrides):
    base = {"chave": "agenda.habilitada", "valor": True, "tenant_id": "tenant-1"}
    base.update(overrides)
    return base


def test_sem_authorization_retorna_401():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.patch("/api/tenant/config", json=_body())
    assert resposta.status_code == 401


def test_authorization_errada_retorna_401():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.patch(
            "/api/tenant/config",
            json=_body(),
            headers={"Authorization": "Bearer chave-errada"},
        )
    assert resposta.status_code == 401


def test_chave_fora_da_whitelist_retorna_400():
    with patch(_PATCH_SETTINGS, _mock_settings()):
        resposta = client.patch(
            "/api/tenant/config",
            json=_body(chave="admin.senha_master"),
            headers=_HEADERS,
        )
    assert resposta.status_code == 400


def test_tenant_inexistente_retorna_404():
    core = MagicMock()
    core.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_CORE_DB, return_value=core):
        resposta = client.patch("/api/tenant/config", json=_body(), headers=_HEADERS)
    assert resposta.status_code == 404


def test_sucesso_atualiza_chave_preservando_config_existente():
    core = _mock_core_com_tenant(config={"handoff": {"habilitado": True}, "nome_agente": "Ana"})

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_CORE_DB, return_value=core):
        resposta = client.patch(
            "/api/tenant/config",
            json=_body(chave="agenda.habilitada", valor=True),
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    payload = resposta.json()
    assert payload["sucesso"] is True
    assert payload["config"] == {
        "handoff": {"habilitado": True},
        "nome_agente": "Ana",
        "agenda": {"habilitada": True},
    }

    core.table.return_value.update.assert_called_once_with(
        {
            "config": {
                "handoff": {"habilitado": True},
                "nome_agente": "Ana",
                "agenda": {"habilitada": True},
            }
        }
    )


def test_sucesso_atualiza_handoff_sem_tocar_agenda_existente():
    core = _mock_core_com_tenant(config={"agenda": {"habilitada": True}})

    with patch(_PATCH_SETTINGS, _mock_settings()), patch(_PATCH_CORE_DB, return_value=core):
        resposta = client.patch(
            "/api/tenant/config",
            json=_body(chave="handoff.habilitado", valor=False),
            headers=_HEADERS,
        )

    assert resposta.status_code == 200
    payload = resposta.json()
    assert payload["config"] == {"agenda": {"habilitada": True}, "handoff": {"habilitado": False}}
