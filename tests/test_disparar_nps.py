"""Testes para o job job_disparar_nps em agents/internal/scheduler.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.internal.scheduler import (
    _disparar_nps_agendamento,
    _disparar_nps_tenant,
    _montar_pergunta_nps,
    job_disparar_nps,
)

_PATCH_CORE = "agents.internal.scheduler.get_supabase"
_PATCH_VERTICAL = "agents.internal.scheduler.get_vertical_supabase"
_PATCH_ENVIAR = "agents.internal.scheduler.enviar_mensagem_tenant"


@pytest.fixture(autouse=True)
def _consentimento_nps_ativo():
    with patch("agents.internal.scheduler.communication_allowed", return_value=True):
        yield


def _mock_core_telefone(telefone: str | None) -> MagicMock:
    core = MagicMock()
    core.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [{"telefone": telefone}] if telefone else []
    )
    return core


def _mock_vertical_dedup(*, existente: bool) -> MagicMock:
    vertical = MagicMock()
    vertical.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [{"id": "nps-existente"}] if existente else []
    )
    return vertical


def _mock_vertical_agendamentos(rows: list[dict]) -> MagicMock:
    vertical = MagicMock()
    vertical.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.return_value.execute.return_value.data = rows
    return vertical


_AGENDAMENTO = {"id": "ag-1", "contato_id": "contato-1"}


# ─── _montar_pergunta_nps ───────────────────────────────────────────────────────


def test_pergunta_usa_nome_do_medico_do_perfil() -> None:
    texto = _montar_pergunta_nps({"perfil_medico": {"nome": "Dr. Ana Lima"}})
    assert "Dr. Ana Lima" in texto


def test_pergunta_fallback_generico_sem_perfil() -> None:
    texto = _montar_pergunta_nps({})
    assert "o médico" in texto


# ─── _disparar_nps_agendamento: disparo, dedup, falha de envio ─────────────────


async def test_disparo_sucesso_envia_e_insere_pendente() -> None:
    core = _mock_core_telefone("+5567999990000")
    vertical = _mock_vertical_dedup(existente=False)
    mock_envia = AsyncMock(return_value=True)

    with patch(_PATCH_ENVIAR, mock_envia):
        await _disparar_nps_agendamento(
            core, vertical, "tenant-1", {"perfil_medico": {"nome": "Dr. Ana"}}, _AGENDAMENTO
        )

    mock_envia.assert_called_once()
    assert mock_envia.call_args.kwargs["numero_destino"] == "+5567999990000"
    assert "Dr. Ana" in mock_envia.call_args.kwargs["texto"]

    insert_payload = vertical.table.return_value.insert.call_args.args[0]
    assert insert_payload == {
        "tenant_id": "tenant-1",
        "contato_id": "contato-1",
        "agendamento_id": "ag-1",
        "score": None,
    }


async def test_envio_falha_nao_insere_nps_respostas() -> None:
    """send-then-insert: se o envio falhar, nada é gravado — próximo ciclo tenta de novo."""
    core = _mock_core_telefone("+5567999990000")
    vertical = _mock_vertical_dedup(existente=False)
    mock_envia = AsyncMock(return_value=False)

    with patch(_PATCH_ENVIAR, mock_envia):
        await _disparar_nps_agendamento(core, vertical, "tenant-1", {}, _AGENDAMENTO)

    mock_envia.assert_called_once()
    vertical.table.return_value.insert.assert_not_called()


async def test_ja_existe_registro_nao_duplica() -> None:
    core = _mock_core_telefone("+5567999990000")
    vertical = _mock_vertical_dedup(existente=True)
    mock_envia = AsyncMock(return_value=True)

    with patch(_PATCH_ENVIAR, mock_envia):
        await _disparar_nps_agendamento(core, vertical, "tenant-1", {}, _AGENDAMENTO)

    mock_envia.assert_not_called()
    vertical.table.return_value.insert.assert_not_called()


async def test_contato_sem_telefone_nao_envia() -> None:
    core = _mock_core_telefone(None)
    vertical = _mock_vertical_dedup(existente=False)
    mock_envia = AsyncMock(return_value=True)

    with patch(_PATCH_ENVIAR, mock_envia):
        await _disparar_nps_agendamento(core, vertical, "tenant-1", {}, _AGENDAMENTO)

    mock_envia.assert_not_called()


async def test_dry_run_nao_envia_nem_grava() -> None:
    core = _mock_core_telefone("+5567999990000")
    vertical = _mock_vertical_dedup(existente=False)
    mock_envia = AsyncMock(return_value=True)

    with patch(_PATCH_ENVIAR, mock_envia):
        await _disparar_nps_agendamento(core, vertical, "tenant-1", {}, _AGENDAMENTO, dry_run=True)

    mock_envia.assert_not_called()
    vertical.table.return_value.insert.assert_not_called()


# ─── _disparar_nps_tenant: janela de busca, delay por tenant, toggle ───────────


async def test_query_filtra_status_presenca_atendido() -> None:
    vertical = _mock_vertical_agendamentos([])
    core = MagicMock()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        await _disparar_nps_tenant("tenant-1", {"notificacoes": {"nps": True}})

    primeiro_eq = vertical.table.return_value.select.return_value.eq.call_args
    segundo_eq = vertical.table.return_value.select.return_value.eq.return_value.eq.call_args
    assert primeiro_eq.args == ("tenant_id", "tenant-1")
    assert segundo_eq.args == ("status_presenca", "atendido")


async def test_delay_horas_default_1_quando_ausente() -> None:
    vertical = _mock_vertical_agendamentos([])
    core = MagicMock()
    antes = datetime.now(timezone.utc)

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        await _disparar_nps_tenant("tenant-1", {"notificacoes": {"nps": True}})

    lte_arg = vertical.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.call_args.args[
        1
    ]
    cutoff_fim = datetime.fromisoformat(lte_arg)
    delta = antes - cutoff_fim
    assert timedelta(minutes=59) < delta < timedelta(minutes=61)


async def test_delay_horas_configuravel_por_tenant() -> None:
    """Tenant com delay=3 usa uma janela diferente de um tenant com delay=1."""
    vertical_delay1 = _mock_vertical_agendamentos([])
    vertical_delay3 = _mock_vertical_agendamentos([])
    core = MagicMock()

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical_delay1),
    ):
        await _disparar_nps_tenant(
            "tenant-1", {"nps": {"delay_horas": 1}, "notificacoes": {"nps": True}}
        )
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical_delay3),
    ):
        await _disparar_nps_tenant(
            "tenant-3", {"nps": {"delay_horas": 3}, "notificacoes": {"nps": True}}
        )

    lte1 = vertical_delay1.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.call_args.args[
        1
    ]
    lte3 = vertical_delay3.table.return_value.select.return_value.eq.return_value.eq.return_value.gte.return_value.lte.call_args.args[
        1
    ]
    cutoff1 = datetime.fromisoformat(lte1)
    cutoff3 = datetime.fromisoformat(lte3)
    # cutoff do tenant com delay maior é mais "no passado" — diferença ~2h
    delta = cutoff1 - cutoff3
    assert timedelta(hours=1, minutes=50) < delta < timedelta(hours=2, minutes=10)


async def test_toggle_nps_off_nao_consulta_bd() -> None:
    vertical = _mock_vertical_agendamentos([])
    core = MagicMock()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        await _disparar_nps_tenant("tenant-1", {"notificacoes": {"nps": False}})

    vertical.table.assert_not_called()


async def test_erro_em_um_candidato_nao_aborta_os_demais() -> None:
    candidatos = [
        {"id": "ag-erro", "contato_id": "c-erro"},
        {"id": "ag-ok", "contato_id": "c-ok"},
    ]
    vertical = _mock_vertical_agendamentos(candidatos)
    core = MagicMock()
    chamados: list[str] = []

    async def _fake_disparo(core_arg, vertical_arg, tenant_id, config, agendamento, **kwargs):
        if agendamento["id"] == "ag-erro":
            raise RuntimeError("falha de banco simulada")
        chamados.append(agendamento["id"])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch("agents.internal.scheduler._disparar_nps_agendamento", _fake_disparo),
    ):
        await _disparar_nps_tenant("tenant-1", {"notificacoes": {"nps": True}})

    assert chamados == ["ag-ok"]


# ─── job_disparar_nps: isolamento entre tenants ────────────────────────────────


async def test_job_disparar_nps_falha_em_tenant_nao_aborta_outro() -> None:
    tenants = [
        {"id": "t1", "nome": "Falha", "config": {}},
        {"id": "t2", "nome": "OK", "config": {}},
    ]
    chamadas: list[str] = []

    async def _fake_tenant(tenant_id, config, *, dry_run=False):
        if tenant_id == "t1":
            raise RuntimeError("erro simulado")
        chamadas.append(tenant_id)

    with (
        patch("agents.internal.scheduler._buscar_tenants_ativos", return_value=tenants),
        patch("agents.internal.scheduler._disparar_nps_tenant", _fake_tenant),
    ):
        await job_disparar_nps()

    assert chamadas == ["t2"]
