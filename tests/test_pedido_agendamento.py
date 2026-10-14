"""Pedido de agendamento coletado pelo agente (item 2.3)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.nodes import PEDIDO_TOOLS, _pedido_pela_bia, _tools_disponiveis
from tools.verticals.clinica.pedido_agendamento import _make_registrar_pedido_agendamento

TENANT = "t-1"


def _state(agenda: dict, handoff: bool = True) -> dict:
    return {
        "vertical": "clinica",
        "tenant_id": TENANT,
        "tenant_config": {"agenda": agenda, "handoff": {"habilitado": handoff}},
    }


def _tools(nomes):
    return [SimpleNamespace(name=n) for n in nomes]


TODAS = ["registrar_pedido_agendamento", "transferir_para_atendente", "buscar_slots_disponiveis"]


@pytest.mark.parametrize(
    ("agenda", "esperado"),
    [
        ({}, False),  # padrão: desligado
        ({"pedido_pela_bia": True}, True),
        ({"pedido_pela_bia": True, "habilitada": True}, False),  # online ligado: agente já marca
        ({"pedido_pela_bia": False}, False),
    ],
)
def test_trava_do_pedido(agenda, esperado):
    assert _pedido_pela_bia(_state(agenda)) is esperado
    with patch("core.nodes.registry") as reg:
        reg.get_tools.return_value = _tools(TODAS)
        nomes = {t.name for t in _tools_disponiveis(_state(agenda))}
    assert (PEDIDO_TOOLS <= nomes) is esperado


def _vertical(existente: list):
    client = MagicMock()
    tabela = client.table.return_value
    tabela.select.return_value.eq.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = existente
    return client, tabela


async def _chamar(client, **kwargs):
    tool = _make_registrar_pedido_agendamento(client)(TENANT)
    args = {
        "contato_id": "c-1",
        "nome_paciente": "  Maria   da Silva ",
        "data_nascimento": "01/02/1980",
        "convenio": "Unimed",
        "tipo_consulta": "primeira_consulta",
        "preferencia_horario": "terça de manhã",
    }
    args.update(kwargs)
    return await tool.ainvoke(args)


async def test_cria_pedido_pendente_com_dados_limpos():
    client, tabela = _vertical([])
    resposta = await _chamar(client)
    inserido = tabela.insert.call_args.args[0]
    assert inserido["tenant_id"] == TENANT and inserido["contato_id"] == "c-1"
    assert inserido["status"] == "pendente"
    assert inserido["nome_paciente"] == "Maria da Silva"
    assert inserido["tipo_consulta"] == "primeira_consulta"
    assert "Não prometa" in resposta


async def test_atualiza_pedido_pendente_existente_em_vez_de_duplicar():
    client, tabela = _vertical([{"id": "p-9"}])
    await _chamar(client, tipo_consulta="qualquer")
    tabela.insert.assert_not_called()
    atualizado = tabela.update.call_args.args[0]
    assert atualizado["tipo_consulta"] == "outro"  # tipo desconhecido vira 'outro'


async def test_sem_nome_pede_antes_de_registrar():
    client, tabela = _vertical([])
    resposta = await _chamar(client, nome_paciente="   ")
    tabela.insert.assert_not_called()
    assert "nome completo" in resposta


async def test_falha_no_banco_nao_derruba_a_conversa():
    client = MagicMock()
    client.table.side_effect = RuntimeError("banco fora")
    resposta = await _chamar(client)
    assert "transfira para a secretária" in resposta
