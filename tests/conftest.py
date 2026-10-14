"""Fixtures compartilhadas dos testes."""

from unittest.mock import patch

import psycopg
import pytest

from db import client as _db_client


@pytest.fixture(autouse=True)
def _proibe_banco_real(monkeypatch):
    """Nenhum teste pode tocar um banco real — nem com um .env presente na máquina.

    Qualquer tentativa de criar cliente Supabase (banco único; o do vertical é o
    mesmo) ou conexão Postgres direta falha na hora, com mensagem clara. Testes devem mockar
    get_supabase/get_vertical_supabase no módulo que os usa.
    """

    def _bloqueado(*_args, **_kwargs):
        raise RuntimeError(
            "Teste tentou conectar a um banco REAL (Supabase/Postgres). "
            "Mocke get_supabase/get_vertical_supabase no módulo testado."
        )

    async def _bloqueado_async(*args, **kwargs):
        _bloqueado(*args, **kwargs)

    _db_client.get_supabase.cache_clear()
    monkeypatch.setattr(_db_client, "create_client", _bloqueado)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _bloqueado_async)
    yield
    _db_client.get_supabase.cache_clear()


@pytest.fixture(autouse=True)
def _neutraliza_crm_fire_and_forget():
    """Impede que as notificações best-effort ao CRM (integrations.crm_leads)
    agendem tasks/HTTP reais durante os testes — senão a task de background pode
    consumir mocks de httpx compartilhados por outros testes (ex.: agendamento).
    Fecha a coroutine recebida para não gerar 'coroutine never awaited'.

    Testes que precisam do comportamento REAL de _disparar capturam a função
    original no import (ver tests/test_crm_leads.py::_disparar_real)."""

    def _noop(coro):
        try:
            coro.close()
        except Exception:
            pass

    with patch("integrations.crm_leads._disparar", side_effect=_noop) as m:
        yield m


@pytest.fixture(autouse=True)
def _sem_espera_de_rajada(monkeypatch):
    """O buffer de rajada (gateway/rajada.py) espera 3 s de silêncio antes de
    responder. Nos testes, responde na hora — quem testa a espera liga de volta."""
    from app.config import settings

    monkeypatch.setattr(settings, "debounce_mensagens_segundos", 0.0)
    monkeypatch.setattr(settings, "debounce_maximo_segundos", 0.0)
    from gateway import rajada

    rajada._filas.clear()
    yield
    rajada._filas.clear()
