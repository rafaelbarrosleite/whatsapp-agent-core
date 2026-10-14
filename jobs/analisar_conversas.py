"""jobs/analisar_conversas.py — Fase 4 / B2: a IA lê a conversa e age.

Job periódico que varre as conversas em atendimento humano e, com um LLM,
interpreta o que aconteceu entre atendente e lead:
  - consulta confirmada (com data+hora) → registra a consulta no paciente certo,
    promove a paciente e move o lead para consulta_agendada;
  - agendamento para OUTRA pessoa → cria contato+lead do paciente terceiro e
    registra a consulta NELE (não em quem contatou);
  - encaixe/espera → coloca o lead na fila de espera (tag fila_espera_em, FIFO).

Automático com revisão fácil: cada ação grava uma NOTA INTERNA na conversa
(mensagens.papel='sistema', nunca enviada ao WhatsApp) que a atendente vê no
CRM e pode DESFAZER. One-shot por conversa (marcas em conversas.metadata).

Roda a cada ~10min (scheduler agente). Toggle por tenant:
config["notificacoes"]["ia_analise"] (PADRÃO DESLIGADO: só roda com true explícito;
ela cria consultas e move o funil, então nenhuma clínica nova liga sem pedir).
"""

import logging
from datetime import datetime, timedelta, timezone

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config import settings
from core.fuso import descrever_fuso, fuso_da_config
from core.registro_painel import registrar_uso_ia
from db.client import get_supabase
from db.conversas import salvar_mensagem
from db.registro_consulta import (
    criar_paciente_terceiro,
    marcar_fila_espera,
    registrar_consulta,
)
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

# Haiku 4.5 ($1/M in, $5/M out) em vez de Sonnet 4.6 ($3/$15): extração
# estruturada de fatos explícitos é tarefa de classificação, não de raciocínio.
# 3x mais barato no input; Haiku 4.5 suporta structured outputs.
_MODEL = "claude-haiku-4-5"
_JANELA_HORAS = 72  # só conversas com atividade recente
_MAX_POR_RODADA = 25
_MAX_MSGS = 60

# Marca até QUAL MENSAGEM a conversa já foi analisada (created_at da última
# mensagem lida). NÃO usar conversas.updated_at como marca-d'água: gravar o
# próprio metadata dispara o trigger conversas_updated_at, que bumpa updated_at
# alguns ms DEPOIS do timestamp gerado no Python — a conversa parecia "nova"
# para sempre e era reanalisada a cada rodada (custo recorrente com Sonnet).
_MARCA_DAGUA = "ia_analisada_ate_msg"


class AnaliseConversa(BaseModel):
    """Extração estruturada do que a conversa atendente↔lead indica."""

    consulta_confirmada: bool = Field(
        description="True SOMENTE se o atendente confirmou uma consulta AGENDADA com data E "
        "horário definidos nesta conversa. Se ainda está negociando, sugerindo "
        "horários ou sem confirmação clara, use False."
    )
    data: str | None = Field(
        None,
        description="Data da consulta no formato YYYY-MM-DD, se confirmada. Resolva datas "
        "relativas usando a data de hoje informada.",
    )
    hora: str | None = Field(None, description="Horário da consulta em HH:MM (24h), se confirmada.")
    convenio: str | None = Field(
        None, description="Convênio da consulta, se citado (ex.: Bradesco Saúde, Particular)."
    )
    paciente_terceiro: bool = Field(
        description="True se a consulta é para OUTRA pessoa, diferente de quem está conversando "
        "no WhatsApp."
    )
    paciente_nome: str | None = Field(
        None, description="Nome completo do paciente real (o terceiro), se identificado."
    )
    paciente_telefone: str | None = Field(
        None,
        description="Telefone do paciente real, se informado (pode ser o mesmo de quem contatou).",
    )
    paciente_nascimento: str | None = Field(
        None, description="Data de nascimento do paciente real em YYYY-MM-DD, se informada."
    )
    indicado_por: str | None = Field(None, description="Quem indicou o paciente, se citado.")
    em_espera: bool = Field(
        description="True se o paciente ficou aguardando encaixe/vaga (ex.: pediu horário mais "
        "cedo, entrou em lista de espera)."
    )
    resumo: str = Field(description="Uma frase curta explicando o que foi identificado.")


_SYSTEM = (
    "Você é um analista do CRM de uma clínica. Lê a conversa entre a clínica "
    "(Assistente = assistente virtual; Atendente = secretária humana) e o lead/paciente, "
    "e extrai APENAS fatos explícitos. Não invente. Só marque consulta_confirmada "
    "quando o atendente confirmou de fato um agendamento com data e horário claros. "
    "Se a conversa fala de agendar para outra pessoa, marque paciente_terceiro e "
    "extraia os dados dessa pessoa. Datas relativas devem ser resolvidas com a data de hoje."
)

_MAPA_PAPEL = {
    "usuario": "Paciente",
    "paciente": "Paciente",
    "agente": "Assistente",
    "atendente": "Atendente",
}


def _ia_ativa(config: dict) -> bool:
    return ((config or {}).get("notificacoes") or {}).get("ia_analise") is True


def _montar_transcricao(mensagens: list[dict]) -> str:
    linhas = []
    for m in mensagens[-_MAX_MSGS:]:
        papel = _MAPA_PAPEL.get(m.get("papel"))
        if not papel:
            continue  # ignora notas internas (papel='sistema') e afins
        texto = (m.get("conteudo") or "").strip()
        if texto:
            linhas.append(f"{papel}: {texto}")
    return "\n".join(linhas)


async def _analisar_uma(core, vertical, conversa: dict, config: dict, *, dry_run: bool) -> bool:
    """Analisa uma conversa. Retorna True se o LLM foi de fato chamado; False se
    a conversa foi pulada (sem transcrição, ou nada novo desde a última análise)
    ou em dry-run. O chamador usa isso para contar o teto de análises/rodada."""
    tenant_id = conversa["tenant_id"]
    conversa_id = conversa["id"]
    contato_caller = conversa["contato_id"]
    meta = conversa.get("metadata") or {}

    mensagens = (
        core.table("mensagens")
        .select("papel, conteudo, created_at")
        .eq("conversa_id", conversa_id)
        .order("created_at", desc=False)
        .limit(200)
        .execute()
    ).data or []
    transcricao = _montar_transcricao(mensagens)
    if not transcricao:
        return False

    # Nada novo desde a última análise? Não chama o LLM. A marca d'água é o
    # created_at da última mensagem (imutável), não o updated_at da conversa
    # — ver comentário em _MARCA_DAGUA.
    ultima_msg_em = (mensagens[-1].get("created_at") or "") if mensagens else ""
    ja_analisado_ate = meta.get(_MARCA_DAGUA)
    if ja_analisado_ate and ultima_msg_em and ja_analisado_ate >= ultima_msg_em:
        return False

    fuso = fuso_da_config(config)
    hoje = datetime.now(fuso).strftime("%Y-%m-%d")
    # include_raw: a resposta crua traz os tokens para o painel de custo.
    llm = ChatAnthropic(model=_MODEL, api_key=settings.anthropic_api_key).with_structured_output(
        AnaliseConversa, include_raw=True
    )
    prompt = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(
            content=(
                f"Data de hoje: {hoje} (fuso {descrever_fuso(fuso)}).\n\nConversa:\n{transcricao}"
            )
        ),
    ]
    saida = await llm.ainvoke(prompt)
    registrar_uso_ia(tenant_id, "analise", _MODEL, saida.get("raw"))
    analise: AnaliseConversa | None = saida.get("parsed")
    if analise is None:
        raise saida.get("parsing_error") or ValueError("Análise sem resultado estruturado")

    if dry_run:
        log.info("[DRY-RUN] conversa=%s | %s", conversa_id, analise.model_dump())
        return False

    agiu = False
    alvo_contato = contato_caller

    # Paciente terceiro: cria o lead do paciente real (uma vez por conversa).
    terceiro_ok = (
        analise.paciente_terceiro
        and analise.paciente_nome
        and (analise.paciente_nascimento or analise.convenio or analise.paciente_telefone)
    )
    if terceiro_ok:
        terceiro_id = meta.get("ia_terceiro_contato_id")
        if not terceiro_id:
            caller = (
                core.table("contatos")
                .select("telefone, nome")
                .eq("id", contato_caller)
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            ).data
            resp_tel = caller[0].get("telefone") if caller else None
            resp_nome = caller[0].get("nome") if caller else None
            terceiro_id = criar_paciente_terceiro(
                core,
                vertical,
                tenant_id=tenant_id,
                nome=analise.paciente_nome,
                telefone=analise.paciente_telefone,
                convenio=analise.convenio,
                nascimento=analise.paciente_nascimento,
                indicado_por=analise.indicado_por,
                responsavel_contato_id=contato_caller,
                responsavel_telefone=resp_tel,
                responsavel_nome=resp_nome,
            )
            meta["ia_terceiro_contato_id"] = terceiro_id
            salvar_mensagem(
                core,
                tenant_id,
                conversa_id,
                "sistema",
                f"👤 Paciente *{analise.paciente_nome}* cadastrado automaticamente como lead "
                "separado "
                "(consulta é para outra pessoa).",
                {"tipo": "paciente_terceiro_criado", "contato_paciente": terceiro_id},
            )
            agiu = True
        alvo_contato = terceiro_id

    # Consulta confirmada com data+hora → registra no paciente correto (one-shot).
    if (
        analise.consulta_confirmada
        and analise.data
        and analise.hora
        and not meta.get("ia_consulta_criada")
    ):
        agendamento_id = registrar_consulta(
            core,
            vertical,
            tenant_id=tenant_id,
            contato_id=alvo_contato,
            data=analise.data,
            hora=analise.hora,
            fuso=fuso,
            convenio=analise.convenio,
        )
        meta["ia_consulta_criada"] = True
        meta["ia_agendamento_id"] = agendamento_id
        salvar_mensagem(
            core,
            tenant_id,
            conversa_id,
            "sistema",
            f"🗓️ Consulta agendada automaticamente pelo sistema para *{analise.data} às "
            f"{analise.hora}* "
            f"({analise.convenio or 'Particular'}). Confira os detalhes.",
            {
                "tipo": "consulta_criada",
                "agendamento_id": agendamento_id,
                "contato_paciente": alvo_contato,
            },
        )
        agiu = True

    # Encaixe/espera: entra na FILA DE ESPERA (tag, não estágio).
    if analise.em_espera and not meta.get("ia_espera"):
        marcar_fila_espera(vertical, tenant_id, alvo_contato)
        meta["ia_espera"] = True
        salvar_mensagem(
            core,
            tenant_id,
            conversa_id,
            "sistema",
            "⏳ Paciente adicionado à *fila de espera (encaixe)*.",
            {"tipo": "fila_espera", "contato_paciente": alvo_contato},
        )
        agiu = True

    # Marca até qual mensagem analisamos (idempotente entre rodadas) + carimbo
    # de auditoria. Só o primeiro é usado na decisão de re-analisar.
    meta[_MARCA_DAGUA] = ultima_msg_em
    meta["ia_analisada_em"] = datetime.now(timezone.utc).isoformat()
    core.table("conversas").update({"metadata": meta}).eq("id", conversa_id).eq(
        "tenant_id", tenant_id
    ).execute()
    log.info("analisar_conversas: conversa=%s analisada | agiu=%s", conversa_id, agiu)
    return True


async def analisar_conversas(*, dry_run: bool = False) -> None:
    """Analisa conversas em atendimento humano com mensagens novas desde a
    última análise. Cada ação vira nota interna revisável no CRM."""
    core = get_supabase()
    vertical = get_vertical_supabase()

    tenants = {
        t["id"]: (t.get("config") or {})
        for t in (core.table("tenants").select("id, config").eq("ativo", True).execute().data or [])
    }
    if not tenants:
        return

    limite_janela = (datetime.now(timezone.utc) - timedelta(hours=_JANELA_HORAS)).isoformat()
    convs = (
        core.table("conversas")
        .select("id, tenant_id, contato_id, metadata, updated_at")
        .eq("estado", "aguardando_humano")
        .gte("updated_at", limite_janela)
        .in_("tenant_id", list(tenants.keys()))
        .order("updated_at", desc=True)
        .limit(150)
        .execute()
    ).data or []

    analisadas = 0
    for conv in convs:
        if analisadas >= _MAX_POR_RODADA:
            break
        config = tenants.get(conv["tenant_id"], {})
        if not _ia_ativa(config):
            continue
        # A decisão de (re)analisar vive em _analisar_uma, que compara a marca
        # d'água com o created_at da última mensagem. Só conta no teto da rodada
        # quando o LLM foi realmente chamado.
        try:
            if await _analisar_uma(core, vertical, conv, config, dry_run=dry_run):
                analisadas += 1
        except Exception:
            log.exception("analisar_conversas: erro na conversa=%s", conv.get("id"))

    log.info(
        "analisar_conversas: %d conversa(s) analisada(s)%s",
        analisadas,
        " [dry-run]" if dry_run else "",
    )
