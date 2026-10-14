from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tools.verticals.clinica.agenda_demo import (
    _make_buscar_agendamentos,
    _make_buscar_slots_disponiveis,
    _make_criar_agendamento,
)

_UTC_MENOS_3 = timezone(timedelta(hours=-3))
# Datas no futuro para o teste não depender do dia em que roda.
_SEGUNDA = "2030-01-07"
_SABADO = "2030-01-12"


@pytest.fixture(autouse=True)
def _fuso_da_clinica():
    with patch("tools.verticals.clinica.agenda_demo.fuso_do_tenant", return_value=_UTC_MENOS_3):
        yield


def _client(ocupados: list[str] | None = None) -> MagicMock:
    """Mock do vertical: a consulta de agendamentos devolve `ocupados`."""
    client = MagicMock()
    consulta = client.table.return_value.select.return_value.eq.return_value.in_.return_value
    consulta.gte.return_value.lt.return_value.execute.return_value.data = [
        {"data_hora": d} for d in (ocupados or [])
    ]
    return client


def _core(contato_existe: bool = True) -> MagicMock:
    core = MagicMock()
    (
        core.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [{"id": "c1"}] if contato_existe else []
    return core


def test_slots_pulam_fim_de_semana_e_horario_ocupado():
    # 08:00 de segunda no fuso da clínica (-3) = 11:00 UTC: ocupado.
    client = _client(ocupados=["2030-01-07T11:00:00+00:00"])
    buscar = _make_buscar_slots_disponiveis(client)("t1")
    resultado = buscar.invoke({"data_inicio": _SEGUNDA, "data_fim": _SEGUNDA})
    assert "Segunda 07/01/2030 às 08h00" not in resultado
    assert "Segunda 07/01/2030 às 08h30" in resultado
    assert client.table.return_value.select.return_value.eq.call_args.args == ("tenant_id", "t1")

    sem_vaga = _make_buscar_slots_disponiveis(_client())("t1").invoke(
        {"data_inicio": _SABADO, "data_fim": _SABADO}
    )
    assert "Não há horários" in sem_vaga


def test_slots_recusam_datas_invalidas():
    buscar = _make_buscar_slots_disponiveis(_client())("t1")
    assert "inválidas" in buscar.invoke({"data_inicio": "07/01/2030", "data_fim": _SEGUNDA})


def test_criar_grava_no_fuso_da_clinica_com_tenant_do_codigo():
    client = _client()
    criar = _make_criar_agendamento(client, _core())("t1")
    resultado = criar.invoke({"contato_id": "c1", "data": _SEGUNDA, "horario": "14:30"})
    assert resultado == "Consulta marcada: Segunda 07/01/2030 às 14h30."
    payload = client.table.return_value.insert.call_args.args[0]
    assert payload["tenant_id"] == "t1"
    assert payload["data_hora"] == "2030-01-07T14:30:00-03:00"


def test_criar_recusa_contato_de_outra_clinica():
    client = _client()
    criar = _make_criar_agendamento(client, _core(contato_existe=False))("t1")
    resultado = criar.invoke({"contato_id": "c-de-outra", "data": _SEGUNDA, "horario": "14:30"})
    assert "não encontrado" in resultado
    client.table.return_value.insert.assert_not_called()


@pytest.mark.parametrize("horario", ["12:30", "07:00", "14:15"])
def test_criar_recusa_horario_fora_da_agenda(horario):
    client = _client()
    criar = _make_criar_agendamento(client, _core())("t1")
    assert "não faz parte" in criar.invoke(
        {"contato_id": "c1", "data": _SEGUNDA, "horario": horario}
    )
    client.table.return_value.insert.assert_not_called()


def test_criar_recusa_horario_ocupado():
    client = _client(ocupados=["2030-01-07T17:30:00+00:00"])  # 14:30 em -3
    criar = _make_criar_agendamento(client, _core())("t1")
    assert "ocupado" in criar.invoke({"contato_id": "c1", "data": _SEGUNDA, "horario": "14:30"})
    client.table.return_value.insert.assert_not_called()


def test_buscar_agendamentos_mostra_no_fuso_da_clinica():
    client = MagicMock()
    (
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.in_.return_value.gte.return_value.order.return_value.execute.return_value.data
    ) = [{"data_hora": "2030-01-07T17:30:00+00:00", "tipo": "Consulta", "status": "agendado"}]
    resultado = _make_buscar_agendamentos(client)("t1").invoke({"contato_id": "c1"})
    assert resultado == "📅 Segunda 07/01/2030 às 14h30 — Consulta"
