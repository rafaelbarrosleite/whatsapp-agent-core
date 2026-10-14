"""Guardrail de saída: número na resposta precisa de fonte (Sprint 2 — plano FIAP)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.nodes import guardrail_saida
from security.aderencia import (
    MENSAGEM_CONFIRMAR_COM_EQUIPE,
    MENSAGEM_CONFIRMAR_SEM_HANDOFF,
    numeros_sem_fonte,
)

BASE = (
    "[VALORES] Consulta particular:\nA consulta custa R$ 450,00.\n\n"
    "[PREPARO] Exame de sangue:\nJejum de 8 horas.\n\n"
    "[HORARIOS] Atendimento:\nSegunda a sexta, das 08:00 às 18:00."
)

# ── função pura ─────────────────────────────────────────────────────────────

INVENTADOS = [
    ("A consulta custa R$ 300,00.", "300,00"),
    ("O jejum é de 12 horas.", "12"),
    ("Atendemos a partir das 7h.", "7"),
    ("A infiltração sai por R$ 1.200.", "1.200"),
    ("O retorno é em até 45 dias.", "45"),
]


@pytest.mark.parametrize(("resposta", "numero"), INVENTADOS)
def test_pega_numero_inventado(resposta: str, numero: str) -> None:
    assert numeros_sem_fonte(resposta, [BASE]) == [numero]


SUSTENTADOS = [
    "A consulta particular custa R$ 450,00 😊",
    "O valor é 450 reais.",
    "Faça jejum de 8 horas antes do exame.",
    "Atendemos das 8h às 18h, de segunda a sexta.",
    "Escolha:\n1️⃣ Agendar\n2️⃣ Valores\n3️⃣ Endereço",
    "Olá! Como posso te ajudar hoje?",
]


@pytest.mark.parametrize("resposta", SUSTENTADOS)
def test_aceita_numero_com_fonte_e_texto_sem_numero(resposta: str) -> None:
    assert numeros_sem_fonte(resposta, [BASE]) == []


def test_marcador_de_lista_nao_conta_mas_numero_no_texto_conta() -> None:
    # "1." "2." "3." no início de linha são lista; "3 opções" no meio da frase é
    # informação — e sem fonte. É o comportamento conservador esperado.
    resposta = "Temos 3 opções:\n1. Manhã\n2. Tarde\n3. Noite"
    assert numeros_sem_fonte(resposta, [BASE]) == ["3"]


def test_data_da_agenda_casa_com_formato_iso_da_tool() -> None:
    slots = '{"slots": ["2026-10-14T14:00:00-03:00", "2026-10-14T15:30:00-03:00"]}'
    assert numeros_sem_fonte("Tenho dia 14/10 às 14h ou 15h30.", [slots]) == []


def test_numero_dito_pelo_paciente_e_fonte() -> None:
    assert numeros_sem_fonte("Certo, anotei o dia 22.", ["pode ser dia 22?"]) == []


def test_sem_referencias() -> None:
    assert numeros_sem_fonte("Custa R$ 99.", []) == ["99"]


# ── nó guardrail_saida ──────────────────────────────────────────────────────


def _estado(resposta: str, modo: str | None = None, handoff: bool = False, **extra) -> dict:
    config: dict = {}
    if modo:
        config["guardrail"] = {"aderencia": modo}
    if handoff:
        config["handoff"] = {"habilitado": True}
    base = {
        "messages": [
            HumanMessage(content="quanto custa a consulta?"),
            AIMessage(
                content="", tool_calls=[{"name": "buscar_conhecimento", "args": {}, "id": "c1"}]
            ),
            ToolMessage(content=BASE, tool_call_id="c1"),
            AIMessage(content=resposta, id="resp-1"),
        ],
        "tenant_id": "tenant-123",
        "telefone": "+5567999990000",
        "contato_id": "contato-1",
        "vertical": "clinica",
        "system_prompt": "Você é o agente.",
        "tenant_config": config,
        "intencao": None,
    }
    base.update(extra)
    return base


async def test_resposta_aderente_passa() -> None:
    assert await guardrail_saida(_estado("A consulta custa R$ 450,00.", "bloquear")) == {}


async def test_modo_padrao_so_observa(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        saida = await guardrail_saida(_estado("A consulta custa R$ 300,00."))
    assert saida == {}
    assert "número sem fonte" in caplog.text
    assert "modo=observar" in caplog.text


async def test_modo_bloquear_troca_a_resposta_no_historico() -> None:
    saida = await guardrail_saida(_estado("A consulta custa R$ 300,00.", "bloquear"))
    nova = saida["messages"][0]
    assert nova.content == MENSAGEM_CONFIRMAR_SEM_HANDOFF
    assert nova.id == "resp-1"  # mesmo id → substitui, não acrescenta


async def test_modo_bloquear_com_handoff_avisa_equipe() -> None:
    transferir = MagicMock()
    transferir.name = "transferir_para_atendente"
    transferir.ainvoke = AsyncMock(return_value="ok")
    with patch("core.nodes.registry.get_tools", return_value=[transferir]):
        saida = await guardrail_saida(
            _estado("A consulta custa R$ 300,00.", "bloquear", handoff=True)
        )
    assert saida["messages"][0].content == MENSAGEM_CONFIRMAR_COM_EQUIPE
    args = transferir.ainvoke.call_args[0][0]
    assert args["motivo"].startswith("Confirmar informação")
    assert "300,00" in args["resumo"]


async def test_resposta_anterior_da_bia_nao_vira_fonte() -> None:
    estado = _estado("Como eu disse, custa R$ 300,00.", "bloquear")
    estado["messages"].insert(0, AIMessage(content="A consulta custa R$ 300,00."))
    saida = await guardrail_saida(estado)
    assert saida["messages"][0].content == MENSAGEM_CONFIRMAR_SEM_HANDOFF


async def test_prompt_da_clinica_e_fonte() -> None:
    estado = _estado("O retorno é em 30 dias.", "bloquear", system_prompt="Retorno em 30 dias.")
    assert await guardrail_saida(estado) == {}


async def test_desligado_nao_checa() -> None:
    assert await guardrail_saida(_estado("Custa R$ 300,00.", "desligado")) == {}


async def test_modo_invalido_cai_em_observar() -> None:
    assert await guardrail_saida(_estado("Custa R$ 300,00.", "qualquer")) == {}


async def test_telefone_do_paciente_e_fonte() -> None:
    resposta = "Identifiquei seu WhatsApp como +5567999990000. É para você mesmo?"
    assert await guardrail_saida(_estado(resposta, "bloquear")) == {}


# ── grafo completo ──────────────────────────────────────────────────────────


async def test_grafo_resposta_final_passa_pelo_guardrail_de_saida() -> None:
    from core.graph import criar_grafo

    resposta = AIMessage(content="A consulta custa R$ 300,00.", id="r1")
    with (
        patch(
            "core.graph.carregar_tenant",
            return_value={
                "vertical": "clinica",
                "system_prompt": "Consulta: R$ 450,00.",
                "tenant_config": {"guardrail": {"aderencia": "bloquear"}},
            },
        ),
        patch("core.graph.identificar_contato", return_value={}),
        patch("core.graph.agente", MagicMock(return_value={"messages": [resposta]})),
    ):
        grafo = criar_grafo()
        resultado = await grafo.ainvoke(
            {
                "messages": [HumanMessage(content="quanto custa?")],
                "tenant_id": "t",
                "telefone": "+5567999990000",
                "contato_id": "c",
            },
            config={"configurable": {"thread_id": "t:2"}},
        )
    textos = [m.content for m in resultado["messages"] if isinstance(m, AIMessage)]
    assert textos == [MENSAGEM_CONFIRMAR_SEM_HANDOFF]  # o "300" não ficou no histórico
