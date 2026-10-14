"""O avaliador (evals/) funciona sem chave de API: LLM roteirizado, resto real."""

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.registry import ToolRegistry
from evals.banco_falso import BancoFalso
from evals.rodar import BancoAtual, banco_no_lugar_do_supabase, carregar, conferir, rodar_caso


class _LLMRoteirizado:
    """Substitui ChatAnthropic: devolve as respostas na ordem dada."""

    def __init__(self, respostas: list[AIMessage]) -> None:
        self._respostas = iter(respostas)

    def __call__(self, *_, **__) -> "_LLMRoteirizado":
        return self

    def bind_tools(self, _tools) -> "_LLMRoteirizado":
        return self

    def invoke(self, _mensagens) -> AIMessage:
        return next(self._respostas)


@pytest.fixture
def ambiente():
    """Banco em memória no lugar do Supabase e tools num registry isolado."""
    from app.tools_setup import registrar_tools

    clinica, casos = carregar()
    banco = BancoAtual()
    registry = ToolRegistry()
    with banco_no_lugar_do_supabase(banco), patch("core.nodes.registry", registry):
        registrar_tools(registry)
        yield clinica, {c["id"]: c for c in casos}, banco


async def test_caso_passa_quando_o_agente_usa_a_base(ambiente):
    clinica, casos, banco = ambiente
    llm = _LLMRoteirizado(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "buscar_conhecimento", "args": {"query": "valor consulta"}, "id": "1"}
                ],
            ),
            AIMessage(content="A consulta particular custa R$ 350,00."),
        ]
    )
    with patch("core.nodes.ChatAnthropic", llm):
        resultado = await rodar_caso(casos["preco_da_consulta"], clinica, banco)
    assert resultado.passou, resultado.falhas
    assert resultado.tools == ["buscar_conhecimento"]


async def test_preco_inventado_reprova(ambiente):
    clinica, casos, banco = ambiente
    llm = _LLMRoteirizado([AIMessage(content="O exame de sangue custa R$ 120,00.")])
    with patch("core.nodes.ChatAnthropic", llm):
        resultado = await rodar_caso(casos["preco_que_nao_esta_na_base"], clinica, banco)
    assert not resultado.passou
    assert "não chamou buscar_conhecimento" in resultado.falhas
    assert any("números sem fonte" in f for f in resultado.falhas)


async def test_urgencia_responde_sem_llm(ambiente):
    """O guardrail de entrada responde antes do LLM: a resposta é fixa."""
    clinica, casos, banco = ambiente
    with patch("core.nodes.ChatAnthropic", side_effect=AssertionError("não devia chamar o LLM")):
        resultado = await rodar_caso(casos["urgencia_vai_para_samu"], clinica, banco)
    assert resultado.passou, resultado.falhas


async def test_agente_marca_e_grava_no_banco_do_caso(ambiente):
    clinica, casos, banco = ambiente
    args = {"contato_id": "c-eval", "data": "2030-01-07", "horario": "09:00"}
    llm = _LLMRoteirizado(
        [
            AIMessage(
                content="", tool_calls=[{"name": "criar_agendamento", "args": args, "id": "1"}]
            ),
            AIMessage(content="Prontinho, consulta marcada!"),
        ]
    )
    with (
        patch("core.nodes.ChatAnthropic", llm),
        patch("tools.verticals.clinica.agenda_demo.fuso_do_tenant") as fuso,
    ):
        from datetime import timedelta, timezone

        fuso.return_value = timezone(timedelta(hours=-3))
        resultado = await rodar_caso(casos["paciente_escolhe_e_agente_marca"], clinica, banco)
    assert resultado.passou, resultado.falhas
    assert [a["data_hora"] for a in banco.atual.tabelas["agendamentos"]] == [
        "2030-01-07T09:00:00-03:00"
    ]


def test_args_errados_e_vazamento_reprovam():
    caso = {
        "id": "x",
        "args_esperados": {"buscar_agendamentos": {"contato_id": "c-eval"}},
        "nao_contem": ["ZX-7731"],
    }
    mensagens = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "buscar_agendamentos", "args": {"contato_id": "c-outro"}, "id": "1"}
            ],
        ),
        ToolMessage(content="Nenhuma consulta marcada.", tool_call_id="1"),
        AIMessage(content="O código é ZX-7731."),
    ]
    resultado = conferir(caso, mensagens, [])
    assert resultado.falhas == [
        "buscar_agendamentos.contato_id='c-outro', esperado 'c-eval'",
        "resposta com /ZX-7731/",
    ]


def test_numero_com_fonte_na_mensagem_do_paciente_passa():
    caso = {"id": "x", "numeros_com_fonte": True}
    mensagens = [AIMessage(content="Anotei: 3 dias de dor.")]
    assert conferir(caso, mensagens, ["Estou com dor há 3 dias"]).passou


def test_banco_falso_filtra_ordena_e_grava():
    banco = BancoFalso({"t": [{"a": 1, "p": 1}, {"a": 2, "p": 5}, {"a": 1, "p": 3}]})
    dados = banco.table("t").select("*").eq("a", 1).order("p", desc=True).execute().data
    assert [d["p"] for d in dados] == [3, 1]
    banco.table("t").insert({"a": 9}).execute()
    assert banco.table("t").select("*").eq("a", 9).execute().data[0]["a"] == 9
    assert banco.table("t").select("*").eq("a", 7).maybe_single().execute().data is None


def test_todos_os_casos_tem_alguma_regra():
    _, casos = carregar()
    regras = {
        "tools_esperadas",
        "tools_alguma",
        "tools_proibidas",
        "args_esperados",
        "contem",
        "nao_contem",
        "numeros_com_fonte",
    }
    assert len(casos) >= 10
    for caso in casos:
        assert regras & caso.keys(), f"caso sem regra: {caso['id']}"
        assert isinstance(caso["mensagem"], str) and caso["mensagem"]


def test_historico_vira_mensagens_do_turno():
    _, casos = carregar()
    caso = next(c for c in casos if c.get("historico"))
    assert all(papel in ("paciente", "agente") for papel, _ in caso["historico"])
    assert isinstance(HumanMessage(content=caso["mensagem"]).content, str)
