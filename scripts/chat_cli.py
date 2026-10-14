#!/usr/bin/env python3
"""
Chat CLI — conversa com o agente pelo terminal, sem WhatsApp.

Uso:
  uv run python scripts/chat_cli.py <tenant_id> <telefone>
  uv run python scripts/chat_cli.py          ← solicita interativamente

Requer: .env com ANTHROPIC_API_KEY, SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY,
banco com supabase/schema.sql e um tenant (scripts/seed_tenant_teste.py).

Roda o mesmo grafo e as mesmas tools do servidor. A memória da conversa fica
em RAM (MemorySaver): some quando o chat fecha.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.tools_setup import registrar_tools
from core.graph import RECURSION_LIMIT, criar_grafo
from core.registry import registry


def imprimir_turno(mensagens: list) -> None:
    """Exibe tool calls, resultados e a resposta final do agente."""
    for msg in mensagens:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                print(f"  [tool chamada] {tc['name']}  args={tc['args']}")
        elif isinstance(msg, ToolMessage):
            print(f"  [tool resultado] {msg.content}")

    ultima = mensagens[-1]
    if isinstance(ultima, AIMessage) and ultima.content:
        print(f"\nAgente: {ultima.content}\n")


async def turno(grafo, config: dict, entrada: dict) -> list:
    """Roda um turno e devolve só as mensagens novas deste turno."""
    antes = len((await grafo.aget_state(config)).values.get("messages", []))
    resultado = await grafo.ainvoke(entrada, config=config)
    return resultado["messages"][antes:]


async def main() -> None:
    registrar_tools(registry)

    if len(sys.argv) >= 3:
        tenant_id, telefone = sys.argv[1], sys.argv[2]
    else:
        tenant_id = input("tenant_id : ").strip()
        telefone = input("telefone  : ").strip()

    grafo = criar_grafo()
    # thread_id = (tenant_id, telefone): o mesmo telefone em outra clínica é
    # outra conversa.
    config = {
        "configurable": {"thread_id": f"{tenant_id}:{telefone}"},
        "recursion_limit": RECURSION_LIMIT,
    }

    print(f"\n── Agente conectado {'─' * 36}")
    print(f"   tenant  : {tenant_id}")
    print(f"   telefone: {telefone}")
    print("   Digite 'sair' para encerrar.\n")

    primeiro_turno = True
    while True:
        try:
            texto = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEncerrando.")
            break
        if not texto or texto.lower() in ("sair", "exit", "quit"):
            print("Encerrando.")
            break

        entrada: dict = {"messages": [HumanMessage(content=texto)]}
        if primeiro_turno:
            # Sem checkpoint ainda: o State precisa dos campos obrigatórios.
            entrada |= {"tenant_id": tenant_id, "telefone": telefone, "contato_id": None}
            primeiro_turno = False

        try:
            imprimir_turno(await turno(grafo, config, entrada))
        except Exception as exc:
            print(f"\n[ERRO] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
