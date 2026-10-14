from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core.nodes import (
    agente,
    carregar_tenant,
    depois_do_guardrail,
    deve_usar_tools,
    executar_tools,
    guardrail_entrada,
    guardrail_saida,
    identificar_contato,
)
from core.state import State

# Máximo de iterações agente↔tools por turno — evita loops de raciocínio infinitos.
RECURSION_LIMIT = 10


def criar_grafo(checkpointer=None):
    """Constrói e compila o grafo do agente.

    O checkpointer é injetado pelo chamador para facilitar a troca:
      - desenvolvimento → MemorySaver() (padrão, in-memory)
      - produção       → PostgresSaver(...) (Fase 4)

    O thread_id (chave do checkpointer) deve ser definido pelo chamador
    via config={"configurable": {"thread_id": f"{tenant_id}:{telefone}"}}.
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    builder = StateGraph(State)

    builder.add_node("carregar_tenant", carregar_tenant)
    builder.add_node("identificar_contato", identificar_contato)
    builder.add_node("guardrail_entrada", guardrail_entrada)
    builder.add_node("agente", agente)
    builder.add_node("executar_tools", executar_tools)
    builder.add_node("guardrail_saida", guardrail_saida)

    builder.add_edge(START, "carregar_tenant")
    builder.add_edge("carregar_tenant", "identificar_contato")
    builder.add_edge("identificar_contato", "guardrail_entrada")

    # Urgência/crise → resposta fixa e fim do turno, sem LLM. Resto → agente.
    builder.add_conditional_edges(
        "guardrail_entrada",
        depois_do_guardrail,
        {"agente": "agente", END: END},
    )

    # Loop ReAct: agente decide se usa tools; se sim, executa e volta ao agente.
    builder.add_conditional_edges(
        "agente",
        deve_usar_tools,
        {"executar_tools": "executar_tools", END: "guardrail_saida"},
    )
    builder.add_edge("executar_tools", "agente")
    # Resposta final passa pela checagem de números antes de sair.
    builder.add_edge("guardrail_saida", END)

    return builder.compile(checkpointer=checkpointer)
