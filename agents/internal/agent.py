"""agents/internal/agent.py — Graph LangGraph do Internal Agent (uso exclusivo do médico).

NÃO importar de módulos do agente (agente paciente). Separação arquitetural obrigatória.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.config import settings
from core.modelos import modelo_do_tenant
from core.registro_painel import registrar_uso_ia
from db.client import get_supabase
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

RECURSION_LIMIT = 10
_BR_TZ = timezone(timedelta(hours=-4))

_SYSTEM_PROMPT_TEMPLATE = """\
Você é o assistente interno {descricao_assistente}.

Você NÃO é o agente que atende pacientes. Você é exclusivo para uso do médico.

Sua função é:
- Dar bom dia com a agenda do dia e briefs dos pacientes quando acionado pelo scheduler
- Responder perguntas do médico sobre pacientes, agenda e históricos em linguagem natural
- Lembrar o médico de pontos pessoais e clínicos importantes antes das consultas
- Salvar notas e observações sobre pacientes quando o médico solicitar
- Alertar sobre aniversários, retornos pendentes e follow-ups

Regras de comportamento:
- Seja direto e objetivo — o médico está sempre ocupado
- Nunca revele informações de pacientes de outros médicos (tenant_id sempre isolado)
- Quando não souber algo, diga claramente em vez de inventar
- Ao apresentar a agenda do dia, liste os pacientes em ordem de horário com o brief de cada um \
formatado de forma legível para WhatsApp (use emojis com moderação para separar seções)
- Para paciente novo, destaque com ⭐
- Responda sempre em português brasileiro

{contexto_permanente}\
"""


def _montar_system_prompt(tenant_config: dict) -> str:
    """Monta o system prompt personalizado para o tenant.

    Usa config["perfil_medico"]: {"nome": "...", "especialidade": "...", "clinica": "..."}.
    Fallback genérico quando ausente — nunca menciona nome de médico fixo.
    """
    perfil = (tenant_config or {}).get("perfil_medico") or {}
    nome = (perfil.get("nome") or "").strip()
    especialidade = (perfil.get("especialidade") or "").strip()
    clinica = (perfil.get("clinica") or "").strip()

    descricao_assistente = f"do {nome}" if nome else "do médico desta clínica"

    linhas_ctx = ["Contexto permanente:"]
    if nome:
        linhas_ctx.append(f"- Médico: {nome}")
    if especialidade:
        linhas_ctx.append(f"- Especialidade: {especialidade}")
    if clinica:
        linhas_ctx.append(f"- Clínica: {clinica}")
    if len(linhas_ctx) == 1:
        linhas_ctx.append("- Perfil não configurado (defina config.perfil_medico no tenant)")

    return _SYSTEM_PROMPT_TEMPLATE.format(
        descricao_assistente=descricao_assistente,
        contexto_permanente="\n".join(linhas_ctx),
    )


# ─── State ───────────────────────────────────────────────────────────────────


class InternalState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tenant_id: str


# ─── Tools ───────────────────────────────────────────────────────────────────


@tool
async def tool_agenda_do_dia(
    data: str | None = None,
    config: RunnableConfig = None,
) -> str:
    """Busca a agenda do médico para uma data (YYYY-MM-DD). Se omitida, usa hoje.
    Use quando o médico perguntar sobre a agenda, consultas do dia ou pacientes previstos."""
    from agents.internal.tools import get_agenda_do_dia

    cfg = (config or {}).get("configurable", {})
    tenant_id = cfg.get("tenant_id", "")
    if not tenant_id:
        return "Erro: tenant_id não encontrado na configuração. Contate o suporte."
    perfil_medico = (cfg.get("tenant_config") or {}).get("perfil_medico") or None

    if data:
        try:
            dia = date.fromisoformat(data)
        except ValueError:
            return f"Data inválida: {data!r}. Use o formato YYYY-MM-DD."
    else:
        dia = datetime.now(_BR_TZ).date()

    try:
        agenda = await get_agenda_do_dia(tenant_id=tenant_id, data=dia, perfil_medico=perfil_medico)
    except Exception as e:
        log.error("Erro ao buscar agenda: %s", e, exc_info=True)
        return f"Erro ao buscar agenda: {e}"

    if not agenda:
        return f"Sem agendamentos para {dia.strftime('%d/%m/%Y')}."

    linhas = [f"📅 Agenda de {dia.strftime('%d/%m/%Y')} — {len(agenda)} consulta(s)\n"]
    for item in agenda:
        dt = datetime.fromisoformat(item["data_hora"].replace("Z", "+00:00")).astimezone(_BR_TZ)
        horario = dt.strftime("%Hh%M")
        nome = item.get("nome") or "Paciente sem nome"
        brief = item.get("brief") or {}
        novo = brief.get("tipo") == "novo"

        linha_nome = f"⏰ *{horario} — {nome}*"
        if novo:
            linha_nome += " ⭐"
        linhas.append(linha_nome)

        if brief.get("resumo_clinico"):
            linhas.append(f"  🩺 {brief['resumo_clinico']}")
        if brief.get("pontos_pessoais"):
            pontos = "; ".join(brief["pontos_pessoais"][:3])
            linhas.append(f"  👤 {pontos}")
        if brief.get("o_que_reforcar_hoje"):
            linhas.append(f"  💡 {brief['o_que_reforcar_hoje'][0]}")
        if novo and brief.get("alerta_novo_paciente"):
            linhas.append(f"  ⭐ {brief['alerta_novo_paciente']}")
        linhas.append("")

    return "\n".join(linhas)


@tool
async def tool_buscar_paciente(
    nome_ou_telefone: str,
    config: RunnableConfig = None,
) -> str:
    """Busca um paciente pelo nome ou telefone e retorna dados e brief clínico.
    Use quando o médico pedir informações sobre um paciente específico."""
    from agents.internal.briefs import gerar_patient_brief

    cfg = (config or {}).get("configurable", {})
    tenant_id = cfg.get("tenant_id", "")
    if not tenant_id:
        return "Erro: tenant_id não encontrado na configuração. Contate o suporte."
    perfil_medico = (cfg.get("tenant_config") or {}).get("perfil_medico") or None

    core = get_supabase()
    termo = nome_ou_telefone.strip().replace("%", "").replace("'", "")

    try:
        resultado = (
            core.table("contatos")
            .select("id, nome, telefone, email, data_nascimento")
            .eq("tenant_id", tenant_id)
            .or_(f"nome.ilike.%{termo}%,telefone.like.%{termo}%")
            .limit(5)
            .execute()
        )
    except Exception as e:
        log.error("Erro ao buscar paciente: %s", e, exc_info=True)
        return f"Erro ao buscar paciente: {e}"

    if not resultado.data:
        return f"Nenhum paciente encontrado para: {nome_ou_telefone!r}"

    linhas = [f"Encontrei {len(resultado.data)} paciente(s):\n"]
    for contato in resultado.data:
        nome = contato.get("nome") or "Sem nome"
        tel = contato.get("telefone") or "—"
        linhas.append(f"*{nome}* | {tel}")
        try:
            brief = await gerar_patient_brief(
                contato_id=contato["id"],
                tenant_id=tenant_id,
                perfil_medico=perfil_medico,
            )
            if brief.get("resumo_clinico"):
                linhas.append(f"  🩺 {brief['resumo_clinico']}")
            nc = brief.get("numero_consultas", 0)
            linhas.append(f"  📋 {nc} consulta(s) no histórico")
        except Exception as e:
            log.error("Erro ao gerar brief para %s: %s", contato["id"], e)
        linhas.append("")

    return "\n".join(linhas)


@tool
async def tool_salvar_nota(
    contato_id: str,
    nota: str,
    config: RunnableConfig = None,
) -> str:
    """Salva uma nota clínica ou observação sobre um paciente.
    Use quando o médico disser 'anota aí', 'registra que...' ou similar."""
    tenant_id = (config or {}).get("configurable", {}).get("tenant_id", "")
    if not tenant_id:
        return "Erro: tenant_id não encontrado na configuração. Contate o suporte."

    vertical = get_vertical_supabase()

    try:
        titulo = f"Nota do médico — {datetime.now(_BR_TZ).strftime('%d/%m/%Y')}"
        vertical.table("crm_notificacoes").insert(
            {
                "tenant_id": tenant_id,
                "tipo": "nota_medico",
                "titulo": titulo,
                "mensagem": f"Paciente {contato_id}: {nota}",
            }
        ).execute()
        return "Nota salva com sucesso. ✅"
    except Exception as e:
        log.error("Erro ao salvar nota: %s", e, exc_info=True)
        return f"Erro ao salvar nota: {e}"


@tool
async def tool_pacientes_sem_retorno(
    dias: int = 60,
    config: RunnableConfig = None,
) -> str:
    """Lista pacientes sem agendamento há mais de X dias (padrão: 60).
    Use quando o médico quiser ver quem precisa retornar ou está sumido."""
    tenant_id = (config or {}).get("configurable", {}).get("tenant_id", "")
    if not tenant_id:
        return "Erro: tenant_id não encontrado na configuração. Contate o suporte."

    vertical = get_vertical_supabase()
    core = get_supabase()

    try:
        resultado = (
            vertical.table("agendamentos")
            .select("contato_id, data_hora")
            .eq("tenant_id", tenant_id)
            .order("data_hora", desc=True)
            .execute()
        )
    except Exception as e:
        log.error("Erro ao buscar agendamentos: %s", e, exc_info=True)
        return f"Erro ao buscar agendamentos: {e}"

    ultimo_por_contato: dict[str, datetime] = {}
    for ag in resultado.data or []:
        cid = ag["contato_id"]
        dt = datetime.fromisoformat(ag["data_hora"].replace("Z", "+00:00"))
        if cid not in ultimo_por_contato or dt > ultimo_por_contato[cid]:
            ultimo_por_contato[cid] = dt

    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    sem_retorno = [cid for cid, dt in ultimo_por_contato.items() if dt < limite]

    if not sem_retorno:
        return f"Todos os pacientes têm consulta nos últimos {dias} dias. ✅"

    try:
        contatos_result = (
            core.table("contatos")
            .select("id, nome, telefone")
            .eq("tenant_id", tenant_id)
            .in_("id", sem_retorno[:50])
            .execute()
        )
    except Exception as e:
        log.error("Erro ao buscar contatos: %s", e, exc_info=True)
        return f"Erro ao buscar dados dos pacientes: {e}"

    contatos_map = {c["id"]: c for c in (contatos_result.data or [])}

    linhas = [f"⚠️ {len(sem_retorno)} paciente(s) sem retorno há mais de {dias} dias:\n"]
    for cid in sem_retorno[:20]:
        c = contatos_map.get(cid, {})
        nome = c.get("nome") or "Sem nome"
        tel = c.get("telefone") or "—"
        dt_ultimo = ultimo_por_contato[cid].astimezone(_BR_TZ)
        linhas.append(f"• {nome} | {tel} | último: {dt_ultimo.strftime('%d/%m/%Y')}")

    if len(sem_retorno) > 20:
        linhas.append(f"\n... e mais {len(sem_retorno) - 20} paciente(s).")

    return "\n".join(linhas)


_TOOLS = [
    tool_agenda_do_dia,
    tool_buscar_paciente,
    tool_salvar_nota,
    tool_pacientes_sem_retorno,
]


# ─── Nodes ───────────────────────────────────────────────────────────────────


def _agente_interno(state: InternalState, config: RunnableConfig) -> dict:
    tenant_config = config.get("configurable", {}).get("tenant_config") or {}
    system_prompt = _montar_system_prompt(tenant_config)
    modelo = modelo_do_tenant(tenant_config, "interno")
    llm = ChatAnthropic(model=modelo, api_key=settings.anthropic_api_key).bind_tools(_TOOLS)
    mensagens = [SystemMessage(content=system_prompt)] + list(state["messages"])
    resposta = llm.invoke(mensagens)
    tenant_id = config.get("configurable", {}).get("tenant_id")
    registrar_uso_ia(tenant_id, "interno", modelo, resposta)
    return {"messages": [resposta]}


async def _executar_tools_interno(state: InternalState, config: RunnableConfig) -> dict:
    tools_por_nome = {t.name: t for t in _TOOLS}
    ultima = state["messages"][-1]

    tasks = []
    for tc in ultima.tool_calls:
        if tc["name"] not in tools_por_nome:
            tasks.append(None)
        else:
            # config propagado para que as tools leiam tenant_id do configurable
            tasks.append(tools_por_nome[tc["name"]].ainvoke(tc["args"], config=config))

    resultados = await asyncio.gather(
        *[t for t in tasks if t is not None],
        return_exceptions=True,
    )

    iter_res = iter(resultados)
    tool_messages = []
    for tc, task in zip(ultima.tool_calls, tasks):
        if task is None:
            conteudo = f"Erro: tool '{tc['name']}' não reconhecida"
        else:
            res = next(iter_res)
            conteudo = (
                f"Erro ao executar '{tc['name']}': {res}"
                if isinstance(res, Exception)
                else str(res)
            )
        tool_messages.append(ToolMessage(content=conteudo, tool_call_id=tc["id"]))

    return {"messages": tool_messages}


def _deve_usar_tools(state: InternalState) -> str:
    ultima = state["messages"][-1]
    return "executar_tools" if getattr(ultima, "tool_calls", None) else END


# ─── Graph ────────────────────────────────────────────────────────────────────


def criar_grafo_interno(checkpointer=None):
    """Constrói e compila o grafo do Internal Agent.

    checkpointer=None → MemorySaver (desenvolvimento).
    Em produção injetar o mesmo AsyncPostgresSaver do agente (via lifespan).
    Thread ID obrigatório: "medico:{tenant_id}" — uma thread por tenant.
    Tenant isolado via config["configurable"]["tenant_id"] e ["tenant_config"].
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    builder = StateGraph(InternalState)
    builder.add_node("agente", _agente_interno)
    builder.add_node("executar_tools", _executar_tools_interno)

    builder.add_edge(START, "agente")
    builder.add_conditional_edges(
        "agente",
        _deve_usar_tools,
        {"executar_tools": "executar_tools", END: END},
    )
    builder.add_edge("executar_tools", "agente")

    return builder.compile(checkpointer=checkpointer)


# Singleton substituído pelo lifespan com o checkpointer correto.
# Webhooks que processam mensagens do médico devem usar este grafo.
_grafo_interno = criar_grafo_interno()
