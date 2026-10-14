"""Testes para integrations/google_calendar.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.google_calendar import (
    _get_valid_access_token,
    criar_evento,
    deletar_evento,
)

_PATCH_VERTICAL = "integrations.google_calendar.get_vertical_supabase"
_PATCH_HTTPX = "integrations.google_calendar.httpx.AsyncClient"
_PATCH_SETTINGS = "integrations.google_calendar.settings"
_PATCH_SETTINGS_CLIENT_ID = "integrations.google_calendar.settings.google_client_id"
_PATCH_SETTINGS_CLIENT_SECRET = "integrations.google_calendar.settings.google_client_secret"

_TOKEN_VALIDO = "access-token-abc"
_REFRESH_TOKEN = "refresh-token-xyz"
_CALENDAR_ID = "primary"
_TENANT = "t-1"

_EXPIRA_FUTURO = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
_EXPIRA_PASSADO = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()


def _mock_oauth_row(
    access_token: str = _TOKEN_VALIDO,
    refresh_token: str = _REFRESH_TOKEN,
    token_expira_em: str = _EXPIRA_FUTURO,
    calendar_id: str = _CALENDAR_ID,
) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expira_em": token_expira_em,
        "calendar_id": calendar_id,
    }


def _mock_vertical(row) -> MagicMock:
    """Chain: .select().eq().eq().eq().single().execute().data"""
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data
    ) = row
    return m


def _mock_http_post(json_body: dict) -> AsyncMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body)
    http = AsyncMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.post = AsyncMock(return_value=resp)
    http.delete = AsyncMock(return_value=MagicMock())
    return http


# ─── _get_valid_access_token ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_valid_token_retorna_token_valido() -> None:
    row = _mock_oauth_row(token_expira_em=_EXPIRA_FUTURO)
    vertical = _mock_vertical(row)

    with patch(_PATCH_VERTICAL, return_value=vertical):
        result = await _get_valid_access_token(_TENANT)

    assert result is not None
    token, cal_id = result
    assert token == _TOKEN_VALIDO
    assert cal_id == _CALENDAR_ID


@pytest.mark.asyncio
async def test_get_valid_token_retorna_none_se_nao_conectado() -> None:
    vertical = _mock_vertical(None)

    with patch(_PATCH_VERTICAL, return_value=vertical):
        result = await _get_valid_access_token(_TENANT)

    assert result is None


@pytest.mark.asyncio
async def test_get_valid_token_faz_refresh_se_expirado() -> None:
    row = _mock_oauth_row(token_expira_em=_EXPIRA_PASSADO)
    vertical = _mock_vertical(row)

    novo_token = "novo-access-token"
    http = _mock_http_post({"access_token": novo_token, "expires_in": 3600})

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
        patch(_PATCH_SETTINGS_CLIENT_ID, "client-id"),
        patch(_PATCH_SETTINGS_CLIENT_SECRET, "client-secret"),
    ):
        result = await _get_valid_access_token(_TENANT)

    assert result is not None
    token, _ = result
    assert token == novo_token
    # Deve ter feito POST para renovar o token
    http.post.assert_called_once()
    # Deve ter atualizado o banco com o novo token
    vertical.table.return_value.update.assert_called_once()
    payload = vertical.table.return_value.update.call_args[0][0]
    assert payload["access_token"] == novo_token


@pytest.mark.asyncio
async def test_get_valid_token_retorna_none_se_refresh_falhar() -> None:
    row = _mock_oauth_row(token_expira_em=_EXPIRA_PASSADO)
    vertical = _mock_vertical(row)

    # Resposta do Google sem access_token (erro)
    http = _mock_http_post({"error": "invalid_grant"})

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
        patch(_PATCH_SETTINGS_CLIENT_ID, "id"),
        patch(_PATCH_SETTINGS_CLIENT_SECRET, "secret"),
    ):
        result = await _get_valid_access_token(_TENANT)

    assert result is None


@pytest.mark.asyncio
async def test_get_valid_token_usa_primary_quando_calendar_id_nulo() -> None:
    row = _mock_oauth_row(calendar_id=None)
    vertical = _mock_vertical(row)

    with patch(_PATCH_VERTICAL, return_value=vertical):
        result = await _get_valid_access_token(_TENANT)

    assert result is not None
    _, cal_id = result
    assert cal_id == "primary"


# ─── criar_evento ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_criar_evento_retorna_event_id() -> None:
    event_id = "google-event-abc123"
    http = _mock_http_post({"id": event_id, "status": "confirmed"})
    row = _mock_oauth_row()
    vertical = _mock_vertical(row)

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
    ):
        result = await criar_evento(
            tenant_id=_TENANT,
            titulo="Consulta — João",
            inicio_utc="2026-07-29T08:00:00-04:00",
            fim_utc="2026-07-29T08:30:00-04:00",
        )

    assert result == event_id
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_criar_evento_retorna_none_se_nao_conectado() -> None:
    vertical = _mock_vertical(None)

    with patch(_PATCH_VERTICAL, return_value=vertical):
        result = await criar_evento(
            tenant_id=_TENANT,
            titulo="Consulta",
            inicio_utc="2026-07-29T08:00:00-04:00",
            fim_utc="2026-07-29T08:30:00-04:00",
        )

    assert result is None


@pytest.mark.asyncio
async def test_criar_evento_retorna_none_se_google_nao_retorna_id() -> None:
    http = _mock_http_post({"error": "forbidden"})
    row = _mock_oauth_row()
    vertical = _mock_vertical(row)

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
    ):
        result = await criar_evento(
            tenant_id=_TENANT,
            titulo="Consulta",
            inicio_utc="2026-07-29T08:00:00-04:00",
            fim_utc="2026-07-29T08:30:00-04:00",
        )

    assert result is None


@pytest.mark.asyncio
async def test_criar_evento_envia_campos_corretos() -> None:
    http = _mock_http_post({"id": "evt-1"})
    row = _mock_oauth_row()
    vertical = _mock_vertical(row)

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
    ):
        await criar_evento(
            tenant_id=_TENANT,
            titulo="Consulta — Maria",
            inicio_utc="2026-07-29T08:00:00-04:00",
            fim_utc="2026-07-29T08:30:00-04:00",
            descricao="Paciente: Maria",
            local="Rua das Flores, 100",
        )

    payload = http.post.call_args.kwargs["json"]
    assert payload["summary"] == "Consulta — Maria"
    assert payload["start"]["dateTime"] == "2026-07-29T08:00:00-04:00"
    assert payload["end"]["dateTime"] == "2026-07-29T08:30:00-04:00"
    assert payload["description"] == "Paciente: Maria"
    assert payload["location"] == "Rua das Flores, 100"


# ─── deletar_evento ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deletar_evento_chama_delete() -> None:
    http = _mock_http_post({})
    row = _mock_oauth_row()
    vertical = _mock_vertical(row)

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
    ):
        await deletar_evento(_TENANT, "evt-abc")

    http.delete.assert_called_once()
    url = http.delete.call_args.args[0]
    assert "evt-abc" in url


@pytest.mark.asyncio
async def test_deletar_evento_silencioso_se_falhar() -> None:
    """Exceção em deletar_evento não deve propagar."""
    vertical = _mock_vertical(None)  # não conectado

    with patch(_PATCH_VERTICAL, return_value=vertical):
        await deletar_evento(_TENANT, "evt-xyz")  # não deve levantar


@pytest.mark.asyncio
async def test_deletar_evento_silencioso_se_http_falhar() -> None:
    http = AsyncMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.delete = AsyncMock(side_effect=Exception("network error"))

    row = _mock_oauth_row()
    vertical = _mock_vertical(row)

    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_HTTPX, return_value=http),
    ):
        await deletar_evento(_TENANT, "evt-xyz")  # não deve levantar


# ── Cifra dos tokens (item 1.7) ──────────────────────────────────────────────

_CHAVE_TESTE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


@pytest.mark.asyncio
async def test_linha_em_texto_puro_e_regravada_cifrada_e_token_sai_em_claro():
    from security.cifra_tokens import decifrar, esta_cifrado

    vertical = _mock_vertical(_mock_oauth_row())
    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch("integrations.google_calendar.settings.tokens_crypto_key", _CHAVE_TESTE),
    ):
        resultado = await _get_valid_access_token(_TENANT)

    assert resultado == (_TOKEN_VALIDO, _CALENDAR_ID)
    regravado = vertical.table.return_value.update.call_args.args[0]
    assert esta_cifrado(regravado["access_token"]) and esta_cifrado(regravado["refresh_token"])
    assert _TOKEN_VALIDO not in regravado["access_token"]
    assert (
        decifrar(regravado["refresh_token"], _TENANT, "google_calendar", _CHAVE_TESTE)
        == _REFRESH_TOKEN
    )


@pytest.mark.asyncio
async def test_linha_ja_cifrada_e_lida_sem_regravar():
    from security.cifra_tokens import cifrar

    row = _mock_oauth_row(
        access_token=cifrar(_TOKEN_VALIDO, _TENANT, "google_calendar", _CHAVE_TESTE),
        refresh_token=cifrar(_REFRESH_TOKEN, _TENANT, "google_calendar", _CHAVE_TESTE),
    )
    vertical = _mock_vertical(row)
    with (
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch("integrations.google_calendar.settings.tokens_crypto_key", _CHAVE_TESTE),
    ):
        resultado = await _get_valid_access_token(_TENANT)

    assert resultado == (_TOKEN_VALIDO, _CALENDAR_ID)
    vertical.table.return_value.update.assert_not_called()
