from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class State(TypedDict):
    # Histórico da conversa. add_messages é um reducer: ao invés de substituir a lista,
    # LangGraph *append* as novas mensagens retornadas por cada nó — nunca perde histórico.
    messages: Annotated[list[BaseMessage], add_messages]

    # Identifica o tenant dono desta conversa. Definido na entrada do webhook e
    # nunca alterado. É a âncora de segurança: tudo filtra por este valor.
    tenant_id: str

    # Número de telefone do contato (E.164, ex: "+5511999990000").
    # Usado por identificar_contato para resolver o UUID do contato no banco.
    telefone: str

    # UUID do contato na tabela `contatos`. Começa None; o nó identificar_contato
    # preenche após buscar (ou criar) o contato no banco.
    contato_id: str | None

    # Vertical do tenant (ex: "clinica", "garagem"). Preenchido por carregar_tenant.
    # Determina quais tools o registry vai carregar além das genéricas.
    vertical: str

    # Prompt de sistema do tenant. Preenchido por carregar_tenant.
    # Inserido como SystemMessage antes do histórico em cada invocação do agente.
    system_prompt: str

    # Config livre do tenant (tenants.config, JSONB). Preenchido por carregar_tenant.
    # Hoje usado para: nome_agente (prefixo de mensagens) e handoff.habilitado
    # (libera/bloqueia a tool transferir_para_atendente por tenant).
    tenant_config: dict

    # Intenção detectada pelo nó classificador (ex: "agendar_consulta", "cancelar").
    # None até o nó de classificação rodar (Fase 5). Guia roteamento condicional.
    intencao: str | None
