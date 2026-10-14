"""agents/internal/briefs.py — Gerador de brief de consulta para o médico.

USO EXCLUSIVO DO INTERNAL AGENT. Nunca importar de módulos do agente (agente paciente).
"""

import json
import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from core.registro_painel import registrar_uso_ia
from db.client import get_supabase
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_BRIEF_SYSTEM_PROMPT_TEMPLATE = """\
Você é um assistente médico interno que ajuda {descricao_medico} a se preparar para suas consultas.

Sua tarefa é analisar o histórico de um paciente e gerar um brief de consulta estruturado em JSON.

Regras:
- Seja preciso e direto — o médico lerá isso rapidamente antes da consulta
- Extraia pontos pessoais das conversas (profissão, família, hobbies, eventos de vida mencionados) \
— eles fortalecem o vínculo médico-paciente
- Para "o_que_reforcar_hoje": gere sugestões contextuais com base no histórico clínico e no tempo \
desde a última consulta
- Para paciente novo: em "alerta_novo_paciente" sugira perguntas de onboarding relevantes para \
a especialidade do médico (histórico, queixa principal, expectativas)
- Responda APENAS com o JSON válido, sem markdown, sem explicações
- Se não houver informação suficiente para um campo, use null ou lista vazia []\
"""


def _montar_brief_prompt(perfil_medico: dict | None) -> str:
    """Monta o system prompt do brief com o perfil do médico.

    Fallback genérico quando perfil ausente — nunca menciona nome de médico fixo.
    """
    perfil = perfil_medico or {}
    nome = (perfil.get("nome") or "").strip()
    especialidade = (perfil.get("especialidade") or "").strip()

    if nome and especialidade:
        descricao_medico = f"o {nome}, {especialidade}"
    elif nome:
        descricao_medico = f"o {nome}"
    elif especialidade:
        descricao_medico = f"o médico ({especialidade})"
    else:
        descricao_medico = "o médico"

    return _BRIEF_SYSTEM_PROMPT_TEMPLATE.format(descricao_medico=descricao_medico)


def _brief_vazio(erro: bool = False) -> dict:
    result: dict = {
        "tipo": None,
        "numero_consultas": 0,
        "resumo_clinico": None,
        "ultimo_atendimento": {"data": None, "resumo": None},
        "pontos_pessoais": [],
        "o_que_reforcar_hoje": [],
        "pendencias": [],
        "alerta_novo_paciente": None,
    }
    if erro:
        result["erro"] = True
    return result


def _extrair_texto(content: object) -> str:
    """Normaliza o content do AIMessage para string simples."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            bloco.get("text", "") if isinstance(bloco, dict) else str(bloco) for bloco in content
        )
    return str(content)


def _strip_markdown(texto: str) -> str:
    """Remove cerca de código markdown caso o LLM ignore a instrução."""
    texto = texto.strip()
    if texto.startswith("```"):
        linhas = texto.splitlines()
        # remove primeira linha (```json ou ```) e última (```)
        fim = len(linhas) - 1 if linhas[-1].strip() == "```" else len(linhas)
        texto = "\n".join(linhas[1:fim])
    return texto


async def gerar_patient_brief(
    contato_id: str,
    tenant_id: str,
    *,
    perfil_medico: dict | None = None,
) -> dict:
    """Gera um brief de consulta estruturado para o médico do tenant.

    Busca dados do paciente nos dois bancos (Core + Vertical), monta o contexto
    e chama o LLM para retornar um JSON estruturado com resumo clínico, pontos
    pessoais, sugestões e pendências.

    Args:
        contato_id:    UUID do contato no Core DB.
        tenant_id:     UUID do tenant — primeiro filtro em toda query.
        perfil_medico: dict com nome/especialidade/clinica do médico (config["perfil_medico"]).
                       Usado para personalizar o raciocínio clínico do LLM.
                       Fallback genérico quando ausente.

    Returns:
        Dict com o brief estruturado, ou brief vazio com "erro": True em caso de falha.
    """
    core = get_supabase()
    vertical = get_vertical_supabase()

    # 1. Dados do paciente (Core DB) — select * para capturar todos os campos CRM
    contato_result = (
        core.table("contatos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("id", contato_id)
        .limit(1)
        .execute()
    )
    if not contato_result.data:
        log.warning("Contato não encontrado: %s (tenant %s)", contato_id, tenant_id)
        return _brief_vazio()

    contato = contato_result.data[0]

    # 2. Histórico de conversas + mensagens (Core DB)
    conversas_result = (
        core.table("conversas")
        .select("id, estado, created_at, updated_at")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    conversas_raw = conversas_result.data or []

    conversas_com_mensagens: list[dict] = []
    for conversa in conversas_raw:
        msgs_result = (
            core.table("mensagens")
            .select("papel, conteudo, created_at")
            .eq("tenant_id", tenant_id)
            .eq("conversa_id", conversa["id"])
            .order("created_at", desc=False)
            .execute()
        )
        conversas_com_mensagens.append(
            {
                **conversa,
                "mensagens": msgs_result.data or [],
            }
        )

    # 3. Agendamentos (Vertical DB) — histórico completo para contar consultas
    agendamentos_result = (
        vertical.table("agendamentos")
        .select("id, data_hora, tipo, status, convenio, observacao")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .order("data_hora", desc=True)
        .limit(20)
        .execute()
    )
    agendamentos = agendamentos_result.data or []
    numero_consultas = len(agendamentos)

    # 4. Knowledge blocks do tenant (Vertical DB) — contexto clínico para sugestões
    knowledge_result = (
        vertical.table("agent_knowledge_blocks")
        .select("title, content, category")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .execute()
    )
    knowledge_blocks = knowledge_result.data or []

    # 5. Montar contexto e invocar LLM
    contexto = {
        "paciente": contato,
        "numero_consultas": numero_consultas,
        "agendamentos": agendamentos,
        "conversas": conversas_com_mensagens,
        "contexto_clinico": knowledge_blocks,
    }

    prompt_usuario = (
        "Gere o brief de consulta para este paciente.\n\n"
        "Dados coletados:\n"
        f"{json.dumps(contexto, ensure_ascii=False, default=str, indent=2)}\n\n"
        "Retorne APENAS o JSON com a estrutura solicitada."
    )

    texto = ""
    try:
        llm = ChatAnthropic(model=_MODEL, api_key=settings.anthropic_api_key)
        resposta = await llm.ainvoke(
            [
                SystemMessage(content=_montar_brief_prompt(perfil_medico)),
                HumanMessage(content=prompt_usuario),
            ]
        )
        registrar_uso_ia(tenant_id, "briefing", _MODEL, resposta)
        texto = _extrair_texto(resposta.content)
        texto = _strip_markdown(texto)
        return json.loads(texto)

    except json.JSONDecodeError as e:
        log.error(
            "LLM retornou JSON inválido para contato %s: %s | chars=%d",
            contato_id,
            e,
            len(texto),
        )
        return _brief_vazio(erro=True)

    except Exception as e:
        log.error("Erro ao gerar brief para contato %s: %s", contato_id, e, exc_info=True)
        return _brief_vazio(erro=True)
