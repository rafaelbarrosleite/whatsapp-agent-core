import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import integrations.crm_leads as crm

# Captura a função REAL no import (o conftest autouse patcheia crm._disparar).
from integrations.crm_leads import _disparar as _disparar_real


def _mock_async_client(*, status_code: int | None = None, exc: Exception | None = None):
    """Mock de httpx.AsyncClient usado como context manager async.
    Retorna (ctx, post_mock) — ctx entra no `async with`, post_mock é o .post()."""
    client = MagicMock()
    if exc is not None:
        client.post = AsyncMock(side_effect=exc)
    else:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = "ok"
        client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client.post


# ─── _post_crm (best-effort, nunca propaga) ──────────────────────────────────


async def test_post_crm_sucesso_faz_post_com_auth_e_nao_levanta():
    ctx, post = _mock_async_client(status_code=200)
    with (
        patch("integrations.crm_leads.settings") as s,
        patch("httpx.AsyncClient", return_value=ctx),
    ):
        s.crm_url = "https://crm.example/"
        s.internal_api_key = "segredo"
        await crm._post_crm("/api/leads/link-contato", {"contato_id": "c1"})

    post.assert_awaited_once()
    _, kwargs = post.call_args
    assert kwargs["json"] == {"contato_id": "c1"}
    assert kwargs["headers"] == {"x-internal-key": "segredo"}
    assert post.call_args.args[0] == "https://crm.example/api/leads/link-contato"


async def test_post_crm_erro_de_rede_nao_propaga():
    ctx, _ = _mock_async_client(exc=httpx.ConnectError("sem rede"))
    with (
        patch("integrations.crm_leads.settings") as s,
        patch("httpx.AsyncClient", return_value=ctx),
    ):
        s.crm_url = "https://crm.example"
        s.internal_api_key = "k"
        await crm._post_crm("/api/leads/advance-stage", {"contato_id": "c1"})  # não levanta


async def test_post_crm_timeout_nao_propaga():
    ctx, _ = _mock_async_client(exc=httpx.TimeoutException("timeout"))
    with (
        patch("integrations.crm_leads.settings") as s,
        patch("httpx.AsyncClient", return_value=ctx),
    ):
        s.crm_url = "https://crm.example"
        s.internal_api_key = "k"
        await crm._post_crm("/api/leads/advance-stage", {"contato_id": "c1"})  # não levanta


async def test_post_crm_status_erro_nao_propaga():
    ctx, post = _mock_async_client(status_code=500)
    with (
        patch("integrations.crm_leads.settings") as s,
        patch("httpx.AsyncClient", return_value=ctx),
    ):
        s.crm_url = "https://crm.example"
        s.internal_api_key = "k"
        await crm._post_crm("/api/leads/link-contato", {"contato_id": "c1"})
    post.assert_awaited_once()


async def test_post_crm_sem_crm_url_nao_chama_http():
    with (
        patch("integrations.crm_leads.settings") as s,
        patch("httpx.AsyncClient") as mock_client,
    ):
        s.crm_url = None
        await crm._post_crm("/api/leads/link-contato", {"contato_id": "c1"})
    mock_client.assert_not_called()


# ─── notificar_* delegam ao _disparar com o payload certo ────────────────────
# (o conftest autouse neutraliza _disparar; aqui verificamos o que seria disparado)


def test_notificar_contato_vinculado_monta_call_correta():
    with patch("integrations.crm_leads._post_crm") as post_mock:
        assert crm.notificar_contato_vinculado("t1", "c1", "+5567999991234", "Maria") is None
    post_mock.assert_called_once_with(
        "/api/leads/link-contato",
        {
            "tenant_id": "t1",
            "contato_id": "c1",
            "telefone": "+5567999991234",
            "nome": "Maria",
        },
    )


def test_notificar_estagio_avancado_monta_call_correta():
    with patch("integrations.crm_leads._post_crm") as post_mock:
        assert crm.notificar_estagio_avancado("t1", "c1") is None
    post_mock.assert_called_once_with(
        "/api/leads/advance-stage",
        {
            "tenant_id": "t1",
            "contato_id": "c1",
        },
    )


# ─── _disparar REAL (fire-and-forget) ────────────────────────────────────────


async def test_disparar_real_agenda_e_executa_a_coroutine():
    rodou = {}

    async def coro():
        rodou["ok"] = True

    _disparar_real(coro())
    await asyncio.sleep(0)  # deixa a task rodar
    assert rodou.get("ok") is True


def test_disparar_real_sem_event_loop_nao_propaga():
    """Sem loop em execução, _disparar não pode levantar — garante que os call
    sites nunca quebram por causa da notificação."""

    async def coro():
        return None

    c = coro()
    with patch("asyncio.create_task", side_effect=RuntimeError("no running loop")):
        _disparar_real(c)  # não deve levantar (fecha a coro e loga)
