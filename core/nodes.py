import asyncio
import logging
from datetime import datetime

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langgraph.graph import END

from app.config import settings
from core.modelos import modelo_do_tenant
from core.observability import metrics
from core.registro_painel import registrar_uso_ia
from core.registry import registry
from core.state import State
from db.client import get_supabase
from security.aderencia import (
    MENSAGEM_CONFIRMAR_COM_EQUIPE,
    MENSAGEM_CONFIRMAR_SEM_HANDOFF,
    numeros_sem_fonte,
)
from security.guardrail_entrada import Categoria, classificar_entrada, mensagem_para
from tools.crm.contatos import _make_buscar_contato, _make_criar_contato

# Roteamento de modelo conforme CLAUDE.md:
# Sonnet → agente com tool use e raciocínio (padrão; cada clínica pode trocar
#          na área administrativa — ver core/modelos.py)
# Haiku  → classificação de intenção (Fase 5)
MODEL_CLASSIFICACAO = "claude-haiku-4-5-20251001"

# TODO Fase 5 — nó classificar_intencao (Haiku):
#   Inserir no grafo entre identificar_contato e agente.
#   Recebe a última HumanMessage, chama MODEL_CLASSIFICACAO para detectar
#   state["intencao"] (ex: "agendar", "cancelar", "duvida_geral").
#   O roteamento condicional pós-classificação decide:
#     - intenção trivial sem tools → Haiku responde direto (economia de tokens)
#     - intenção que exige tools/raciocínio → Sonnet (nó agente atual)
#   Ver CLAUDE.md §5 (Economia de Tokens) e §2 (roteamento de modelo).

logger = logging.getLogger(__name__)

# ── Economia de tokens (CLAUDE.md §5) ───────────────────────────────────────
# Janela deslizante do histórico: quantas mensagens do fim vão ao LLM a cada
# turno. Sem isso o histórico cresce sem limite e cada turno reenvia a conversa
# inteira — custo quadrático numa conversa longa. O state (checkpointer) mantém
# o histórico completo; só o que é ENVIADO ao modelo é podado.
_MAX_MSGS_HISTORICO = 30

_SYSTEM_PROMPT_PADRAO = "Você é um assistente de atendimento prestativo e objetivo."

# Tools que só ficam disponíveis se o tenant tiver a feature habilitada em
# tenant_config (tenants.config no banco). Opt-in — padrão é False.
HANDOFF_TOOLS = {"transferir_para_atendente"}
# Pedido de agendamento (item 2.3): o agente coleta os dados e a secretária
# confirma no CRM. Só com agenda.pedido_pela_bia ligado E o agendamento online
# (agenda.habilitada) desligado — com o online ligado o agente já marca sozinha.
PEDIDO_TOOLS = {"registrar_pedido_agendamento"}
AGENDA_TOOLS = {
    "buscar_slots_disponiveis",
    "criar_agendamento",
    "buscar_agendamentos",
}

# Duas variantes: a de cima só faz sentido se transferir_para_atendente
# também estiver disponível neste turno (handoff.habilitado). Instruir o agente
# a chamar uma tool que não está no bind_tools (porque handoff está
# desligado) faria o LLM tentar uma tool inexistente ou inventar uma
# transferência que nunca aconteceu — daí a variante sem handoff abaixo,
# que é o fallback original (ligar pro consultório).
_INSTRUCAO_AGENDA_DESABILITADA_COM_HANDOFF = (
    "\n\nAGENDAMENTO: O agendamento online via sistema está desativado. "
    "Quando o paciente quiser agendar uma consulta, use imediatamente a "
    "tool transferir_para_atendente com role_destino='atendente' e "
    "motivo='Agendamento de consulta'. Não oriente o paciente a ligar "
    "para o consultório — faça a transferência diretamente pelo sistema."
)
# Variantes com a coleta do pedido (agenda.pedido_pela_bia). A coleta vem
# ANTES da transferência, para a secretária receber tudo pronto.
_COLETA_PEDIDO = (
    "\n\nAGENDAMENTO: Quando o paciente quiser agendar uma consulta, colete "
    "com ele, uma pergunta por vez: nome completo de quem vai ser atendido, "
    "data de nascimento, convênio (ou particular), se é primeira consulta ou "
    "retorno e a preferência de dias e turno. Então use a tool "
    "registrar_pedido_agendamento. Nunca prometa data ou horário: a "
    "secretária confirma."
)
_INSTRUCAO_PEDIDO_COM_HANDOFF = _COLETA_PEDIDO + (
    " Depois de registrar, use transferir_para_atendente com "
    "role_destino='atendente', motivo='Agendamento de consulta' e os dados "
    "coletados no resumo."
)
_INSTRUCAO_PEDIDO_SEM_HANDOFF = _COLETA_PEDIDO + (
    " Depois de registrar, avise que a equipe vai entrar em contato por aqui "
    "para confirmar o horário."
)
_INSTRUCAO_AGENDA_DESABILITADA_SEM_HANDOFF = (
    "\n\nAGENDAMENTO: O agendamento online não está disponível. "
    "Oriente o paciente a entrar em contato pelo telefone do "
    "consultório para marcar uma consulta."
)


def _agenda_habilitada(state: State) -> bool:
    return bool((state.get("tenant_config") or {}).get("agenda", {}).get("habilitada", False))


def _handoff_habilitado(state: State) -> bool:
    return bool((state.get("tenant_config") or {}).get("handoff", {}).get("habilitado", False))


def _pedido_pela_bia(state: State) -> bool:
    """Coleta do pedido de agendamento: só com a trava ligada e o online desligado."""
    config = (state.get("tenant_config") or {}).get("agenda", {})
    return bool(config.get("pedido_pela_bia", False)) and not bool(config.get("habilitada", False))


def _tools_disponiveis(state: State) -> list:
    """Resolve as tools do turno: genéricas + vertical, menos as que exigem
    feature flag desabilitada para este tenant.

    Único ponto onde agente() e executar_tools() buscam tools — evita que os
    dois fiquem de acordo "por coincidência" sobre quais tools o tenant pode
    usar neste turno.
    """
    tools = registry.get_tools(state["vertical"], state["tenant_id"])

    if not _handoff_habilitado(state):
        tools = [t for t in tools if t.name not in HANDOFF_TOOLS]

    if not _agenda_habilitada(state):
        tools = [t for t in tools if t.name not in AGENDA_TOOLS]

    if not _pedido_pela_bia(state):
        tools = [t for t in tools if t.name not in PEDIDO_TOOLS]

    return tools


# ---------------------------------------------------------------------------
# Nó: carregar_tenant
# ---------------------------------------------------------------------------


def carregar_tenant(state: State) -> dict:
    """Carrega vertical, system_prompt e tenant_config do tenant a cada turno.

    Reconsulta sempre — não pula quando vertical já está no State. tenant_config
    e system_prompt são mutáveis em runtime (toggle de feature flag na CRM,
    scripts de atualização de prompt) e o checkpoint do LangGraph persiste o
    State indefinidamente entre turns da mesma conversa (mesmo tenant+telefone
    nunca abre thread novo). Cachear esses campos no primeiro turno significa
    que uma mudança de config feita pelo Rafael nunca chega às conversas já
    em andamento. O custo é um select por PK — desprezível frente à chamada do LLM.
    """
    resultado = (
        get_supabase()
        .table("tenants")
        .select("vertical, system_prompt, config")
        .eq("id", state["tenant_id"])
        .limit(1)
        .execute()
    )

    if not resultado.data:
        raise ValueError(f"Tenant não encontrado: {state['tenant_id']}")

    tenant = resultado.data[0]
    return {
        "vertical": tenant["vertical"],
        "system_prompt": tenant.get("system_prompt") or "",
        "tenant_config": tenant.get("config") or {},
    }


# ---------------------------------------------------------------------------
# Nó: identificar_contato
# ---------------------------------------------------------------------------


def identificar_contato(state: State) -> dict:
    """Resolve o contato pelo par (tenant_id + telefone); cria se não existir.

    Pula se contato_id já está no State. O tenant_id é forçado pelas closures
    das tools — nunca depende de decisão do LLM.
    """
    if state.get("contato_id"):
        return {}

    client = get_supabase()
    tenant_id = state["tenant_id"]
    telefone = state["telefone"]

    buscar = _make_buscar_contato(client)(tenant_id)
    contato = buscar.invoke({"telefone": telefone})

    if contato is None:
        criar = _make_criar_contato(client)(tenant_id)
        contato = criar.invoke({"telefone": telefone})

    return {"contato_id": contato["id"]}


# ---------------------------------------------------------------------------
# Nó: guardrail_entrada
# ---------------------------------------------------------------------------

_MOTIVO_HANDOFF = {
    Categoria.urgencia: "URGÊNCIA: paciente relatou possível emergência",
    Categoria.crise: "CRISE: paciente relatou risco à própria vida",
}


def _guardrail_urgencia_habilitado(state: State) -> bool:
    """Ligado por padrão (opt-out): a clínica só desliga com
    config.guardrail.urgencia = false."""
    config = (state.get("tenant_config") or {}).get("guardrail") or {}
    return config.get("urgencia", True) is not False


def _texto_da_ultima_mensagem(mensagens: list) -> str:
    """Texto da mensagem do paciente neste turno. Mensagem com imagem chega
    como lista de blocos — só os blocos de texto contam."""
    if not mensagens or not isinstance(mensagens[-1], HumanMessage):
        return ""
    conteudo = mensagens[-1].content
    if isinstance(conteudo, str):
        return conteudo
    return " ".join(
        bloco.get("text", "")
        for bloco in conteudo
        if isinstance(bloco, dict) and bloco.get("type") == "text"
    )


async def _avisar_equipe(state: State, motivo: str, resumo: str) -> bool:
    """Abre handoff para a equipe, se a clínica usa handoff. Best-effort: a
    resposta fixa ao paciente sai mesmo se o aviso falhar. Devolve se a
    transferência foi tentada (handoff ligado e tool disponível)."""
    if not _handoff_habilitado(state) or not state.get("contato_id"):
        return False
    tools = {t.name: t for t in _tools_disponiveis(state)}
    transferir = tools.get("transferir_para_atendente")
    if transferir is None:
        return False
    try:
        await transferir.ainvoke(
            {
                "contato_id": state["contato_id"],
                "role_destino": "atendente",
                "motivo": motivo,
                # Vai para a fila do CRM (handoff_fila), não para log: a equipe
                # precisa ler o que o paciente escreveu.
                "resumo": resumo,
            }
        )
    except Exception:
        logger.error(
            "Falha ao avisar equipe | tenant=%s | motivo=%s",
            state.get("tenant_id"),
            motivo,
            exc_info=True,
        )
    return True


async def guardrail_entrada(state: State) -> dict:
    """Barreira determinística antes do LLM (CLAUDE.md §4.9).

    Urgência ou crise → resposta fixa (SAMU 192 / CVV 188), sem chamar o LLM,
    e aviso à equipe quando o handoff está ligado. Qualquer outra mensagem
    segue para o agente sem alteração.
    """
    if not _guardrail_urgencia_habilitado(state):
        return {}
    texto = _texto_da_ultima_mensagem(list(state["messages"]))
    classificacao = classificar_entrada(texto)
    if classificacao is None:
        return {}

    # Só categoria e regra no log — o texto é dado de saúde (CLAUDE.md §4.5).
    logger.warning(
        "Guardrail de entrada disparou | tenant=%s | categoria=%s | regra=%s",
        state.get("tenant_id"),
        classificacao.categoria,
        classificacao.regra,
    )
    await _avisar_equipe(
        state,
        _MOTIVO_HANDOFF[classificacao.categoria],
        f"Mensagem do paciente: {texto[:500]}",
    )
    return {"messages": [AIMessage(content=mensagem_para(classificacao.categoria))]}


def depois_do_guardrail(state: State) -> str:
    """Se o guardrail já respondeu (última mensagem é dele), encerra o turno."""
    if isinstance(state["messages"][-1], AIMessage):
        return END
    return "agente"


# ---------------------------------------------------------------------------
# Nó: agente
# ---------------------------------------------------------------------------


def _logar_uso_cache(resposta) -> None:
    """Loga leitura/escrita do prompt cache. É o sinal para saber se o cache
    está funcionando em produção: se cache_read ficar sempre 0 em turnos
    repetidos, algum conteúdo volátil vazou para o bloco estável (o prefixo
    muda e o cache nunca é lido). Best-effort — nunca quebra a resposta."""
    try:
        uso = getattr(resposta, "usage_metadata", None) or {}
        detalhes = uso.get("input_token_details") or {}
        leitura = detalhes.get("cache_read", 0)
        escrita = detalhes.get("cache_creation", 0)
        if leitura or escrita:
            # input_tokens do LangChain é o TOTAL; cache_read/cache_creation são
            # um detalhamento DELE (não parcelas somáveis). Logamos já quebrado
            # para não induzir a leitura errada de que o total seria a soma.
            total = uso.get("input_tokens") or 0
            preco_cheio = max(total - leitura - escrita, 0)
            pct = round(100 * leitura / total) if total else 0
            logger.info(
                "Prompt cache | total=%s | cacheado=%s (%s%%) | preco_cheio=%s | "
                "escrita=%s | output=%s",
                total,
                leitura,
                pct,
                preco_cheio,
                escrita,
                uso.get("output_tokens"),
            )
    except Exception:  # observabilidade nunca pode derrubar o turno
        logger.debug("Falha ao ler usage do prompt cache", exc_info=True)


def _historico_para_llm(mensagens: list) -> list:
    """Janela deslizante: devolve as últimas _MAX_MSGS_HISTORICO mensagens.

    start_on='human' é o que preserva a integridade do turno: a janela nunca
    começa num ToolMessage órfão (tool_result sem o tool_use correspondente),
    que a API rejeita. Best-effort — se a poda falhar ou zerar a lista, devolve
    o histórico inteiro: gastar tokens a mais é melhor que quebrar a conversa.
    """
    if len(mensagens) <= _MAX_MSGS_HISTORICO:
        return list(mensagens)
    try:
        podado = trim_messages(
            mensagens,
            max_tokens=_MAX_MSGS_HISTORICO,
            strategy="last",
            token_counter=len,  # conta MENSAGENS (barato e previsível)
            start_on="human",  # nunca começa em tool_result órfão
            include_system=False,  # o system vai separado, fora da janela
            allow_partial=False,
        )
    except Exception:
        logger.warning("Falha ao podar histórico — enviando completo", exc_info=True)
        return list(mensagens)
    if not podado:
        return list(mensagens)
    logger.info("Histórico podado | %d -> %d mensagens", len(mensagens), len(podado))
    return list(podado)


def agente(state: State) -> dict:
    """Invoca o LLM (Sonnet) com as tools do vertical e o histórico da conversa.

    As tools são obtidas do registry com tenant_id lacrado em closure —
    o LLM nunca vê nem pode alterar o tenant_id. Algumas tools (ex.: handoff)
    só entram na lista se o tenant tiver a feature habilitada — ver _tools_disponiveis.
    """
    tools = _tools_disponiveis(state)

    modelo = modelo_do_tenant(state.get("tenant_config"), "atendimento")
    llm = ChatAnthropic(model=modelo, api_key=settings.anthropic_api_key).bind_tools(tools)

    telefone = state.get("telefone", "desconhecido")
    contato_id = state.get("contato_id")

    # ── Prompt caching (CLAUDE.md §5) ───────────────────────────────────────
    # O cache da Anthropic é prefix match: tools -> system -> messages. O
    # cache_control no 1º bloco cacheia tools + esse bloco. Por isso o system é
    # partido em dois:
    #   [0] ESTÁVEL   — prompt do tenant + instruções de feature flag. Idêntico
    #                   byte a byte entre TODOS os pacientes e turnos do tenant,
    #                   então uma única entrada de cache serve a conversa toda.
    #   [1] VOLÁTIL   — data, telefone e contato_id. Fica DEPOIS do breakpoint;
    #                   se entrasse no bloco estável, cada paciente (e cada dia)
    #                   viraria uma entrada de cache diferente e o cache nunca
    #                   seria lido.
    bloco_estavel = state.get("system_prompt") or _SYSTEM_PROMPT_PADRAO
    if _pedido_pela_bia(state):
        bloco_estavel += (
            _INSTRUCAO_PEDIDO_COM_HANDOFF
            if _handoff_habilitado(state)
            else _INSTRUCAO_PEDIDO_SEM_HANDOFF
        )
    elif not _agenda_habilitada(state):
        bloco_estavel += (
            _INSTRUCAO_AGENDA_DESABILITADA_COM_HANDOFF
            if _handoff_habilitado(state)
            else _INSTRUCAO_AGENDA_DESABILITADA_SEM_HANDOFF
        )

    bloco_volatil = (
        f"---\nCONTEXTO DA CONVERSA:"
        f"\n- Data atual: {datetime.now().strftime('%d/%m/%Y')}"
        f"\n- Número de WhatsApp do paciente: {telefone}"
        f"\n- Você JÁ TEM o número de quem está falando contigo. NUNCA peça o número de telefone."
        '\n- Quando precisar identificar o paciente, diga: "Identifiquei seu WhatsApp como '
        f"{telefone}."
        f' Esse atendimento é para você mesmo ou para outra pessoa?"'
        f"\n- contato_id deste paciente: {contato_id}. Sempre que uma tool pedir contato_id,"
        f" use EXATAMENTE este valor — nunca invente um id."
    )

    system = SystemMessage(
        content=[
            {"type": "text", "text": bloco_estavel, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": bloco_volatil},
        ]
    )
    mensagens = [system] + _historico_para_llm(list(state["messages"]))

    resposta = llm.invoke(mensagens)
    _logar_uso_cache(resposta)
    registrar_uso_ia(state.get("tenant_id"), "atendimento", modelo, resposta)
    return {"messages": [resposta]}


# ---------------------------------------------------------------------------
# Nó: executar_tools
# ---------------------------------------------------------------------------


async def executar_tools(state: State) -> dict:
    """Executa os tool_calls do último AIMessage em paralelo com asyncio.gather.

    Tools desconhecidas são marcadas com None antes do gather — evita KeyError
    e preserva mensagem de erro descritiva. return_exceptions=True garante que
    uma falha numa tool não cancela as demais.
    """
    tools = _tools_disponiveis(state)
    tools_por_nome = {t.name: t for t in tools}

    ultima = state["messages"][-1]

    tasks = []
    for tool_call in ultima.tool_calls:
        nome = tool_call["name"]
        if nome not in tools_por_nome:
            tasks.append(None)
        else:
            tasks.append(tools_por_nome[nome].ainvoke(tool_call["args"]))

    resultados = await asyncio.gather(
        *[t for t in tasks if t is not None],
        return_exceptions=True,
    )

    # Reconstrói o iterador de resultados do gather (None slots não entram nele)
    iter_resultados = iter(resultados)

    tool_messages = []
    for tool_call, task in zip(ultima.tool_calls, tasks):
        nome = tool_call["name"]
        if task is None:
            conteudo = f"Erro: tool '{nome}' não registrada para o vertical '{state['vertical']}'"
        else:
            resultado = next(iter_resultados)
            if isinstance(resultado, Exception):
                logger.error(
                    # Só os NOMES dos argumentos: os valores trazem dado do
                    # paciente (nome, resumo, telefone) e não podem ir ao log.
                    "executar_tools: tool '%s' levantou exceção (tenant_id=%s, arg_keys=%s)",
                    nome,
                    state.get("tenant_id"),
                    sorted((tool_call.get("args") or {}).keys()),
                    exc_info=resultado,
                )
                conteudo = f"Erro ao executar '{nome}': {resultado}"
            else:
                conteudo = str(resultado)
        tool_messages.append(ToolMessage(content=conteudo, tool_call_id=tool_call["id"]))

    return {"messages": tool_messages}


# ---------------------------------------------------------------------------
# Aresta condicional: deve_usar_tools
# ---------------------------------------------------------------------------


def deve_usar_tools(state: State) -> str:
    """Determina o próximo nó após o agente.

    Se o último AIMessage contém tool_calls → executa as tools e volta ao agente.
    Caso contrário → encerra o turno.
    """
    ultima = state["messages"][-1]
    if getattr(ultima, "tool_calls", None):
        return "executar_tools"
    return END


# ---------------------------------------------------------------------------
# Nó: guardrail_saida (aderência dos números — Sprint 2 do plano FIAP)
# ---------------------------------------------------------------------------

_MODOS_ADERENCIA = {"observar", "bloquear", "desligado"}


def _modo_aderencia(state: State) -> str:
    """Padrão 'observar': só registra. 'bloquear' troca a resposta. Começa em
    observar porque o corte ainda não foi calibrado com conversas reais."""
    config = (state.get("tenant_config") or {}).get("guardrail") or {}
    modo = config.get("aderencia", "observar")
    return modo if modo in _MODOS_ADERENCIA else "observar"


def _texto(conteudo) -> str:
    if isinstance(conteudo, str):
        return conteudo
    return " ".join(
        bloco.get("text", "")
        for bloco in conteudo or []
        if isinstance(bloco, dict) and bloco.get("type") == "text"
    )


def _referencias(state: State) -> list[str]:
    """Tudo que pode sustentar um número na resposta: prompt da clínica,
    mensagens do paciente, resultados de tools, a data de hoje e o telefone. Respostas
    anteriores da própria agente NÃO contam — um número inventado num turno não
    pode virar fonte no turno seguinte."""
    referencias = [
        state.get("system_prompt") or "",
        datetime.now().strftime("%d/%m/%Y"),
        # O prompt manda o agente dizer "Identifiquei seu WhatsApp como +55...".
        state.get("telefone") or "",
    ]
    for mensagem in state["messages"]:
        if isinstance(mensagem, (HumanMessage, ToolMessage)):
            referencias.append(_texto(mensagem.content))
    return referencias


async def guardrail_saida(state: State) -> dict:
    """Confere se cada número da resposta final tem fonte (security/aderencia.py)."""
    modo = _modo_aderencia(state)
    ultima = state["messages"][-1]
    if modo == "desligado" or not isinstance(ultima, AIMessage):
        return {}
    sem_fonte = numeros_sem_fonte(_texto(ultima.content), _referencias(state))
    if not sem_fonte:
        return {}

    metrics.increment("aderencia_numeros_sem_fonte")
    logger.warning(
        "Guardrail de saída: número sem fonte | tenant=%s | modo=%s | numeros=%s",
        state.get("tenant_id"),
        modo,
        sem_fonte,
    )
    if modo != "bloquear":
        return {}

    transferiu = await _avisar_equipe(
        state,
        "Confirmar informação: o agente ia citar um valor sem fonte na base",
        f"Resposta retida do agente: {_texto(ultima.content)[:500]}",
    )
    texto = MENSAGEM_CONFIRMAR_COM_EQUIPE if transferiu else MENSAGEM_CONFIRMAR_SEM_HANDOFF
    # Mesmo id → add_messages SUBSTITUI a resposta no histórico: o número
    # inventado não fica no checkpoint para o LLM repetir depois.
    return {"messages": [AIMessage(content=texto, id=ultima.id)]}
