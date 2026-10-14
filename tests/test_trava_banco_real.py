"""A trava do conftest impede qualquer teste de conectar a um banco real,
mesmo com credenciais configuradas (ex.: .env presente na máquina)."""

from unittest.mock import patch

import psycopg
import pytest

from db.client import get_supabase
from db.vertical_client import get_vertical_supabase


def test_get_supabase_real_bloqueado_mesmo_com_credenciais() -> None:
    with (
        patch("db.client.settings.supabase_url", "https://x.supabase.co"),
        patch("db.client.settings.supabase_service_role_key", "chave"),
        pytest.raises(RuntimeError, match="banco REAL"),
    ):
        get_supabase()


def test_get_vertical_supabase_real_bloqueado_mesmo_com_credenciais() -> None:
    # Banco único: o cliente do vertical é o mesmo do core, com a mesma trava.
    with (
        patch("db.client.settings.supabase_url", "https://x.supabase.co"),
        patch("db.client.settings.supabase_service_role_key", "chave"),
        pytest.raises(RuntimeError, match="banco REAL"),
    ):
        get_vertical_supabase()


async def test_conexao_postgres_direta_bloqueada() -> None:
    with pytest.raises(RuntimeError, match="banco REAL"):
        await psycopg.AsyncConnection.connect("postgresql://u:p@localhost:5432/db")
