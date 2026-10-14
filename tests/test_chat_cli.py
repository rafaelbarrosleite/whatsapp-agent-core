"""O chat de terminal roda o grafo de verdade (assíncrono) e mostra só o turno novo."""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from scripts.chat_cli import turno


async def test_chat_cli_roda_dois_turnos_com_memoria() -> None:
    from core.graph import RECURSION_LIMIT, criar_grafo

    respostas = iter([AIMessage(content="Olá! Como posso ajudar?"), AIMessage(content="Claro!")])
    with (
        patch(
            "core.graph.carregar_tenant",
            return_value={"vertical": "clinica", "system_prompt": "", "tenant_config": {}},
        ),
        patch("core.graph.identificar_contato", return_value={"contato_id": "c1"}),
        patch(
            "core.graph.agente", MagicMock(side_effect=lambda _s: {"messages": [next(respostas)]})
        ),
    ):
        grafo = criar_grafo()
        config = {
            "configurable": {"thread_id": "t1:+5511999990000"},
            "recursion_limit": RECURSION_LIMIT,
        }
        primeiro = await turno(
            grafo,
            config,
            {
                "messages": [HumanMessage(content="oi")],
                "tenant_id": "t1",
                "telefone": "+5511999990000",
                "contato_id": None,
            },
        )
        segundo = await turno(grafo, config, {"messages": [HumanMessage(content="quero agendar")]})

    assert [m.content for m in primeiro] == ["oi", "Olá! Como posso ajudar?"]
    assert [m.content for m in segundo] == ["quero agendar", "Claro!"]
    estado = await grafo.aget_state(config)
    assert len(estado.values["messages"]) == 4  # memória entre turnos
