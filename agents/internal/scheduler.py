"""agents/internal/scheduler.py — Jobs agendados do Internal Agent.

Todos os horários em UTC. Clínica piloto = UTC-4 fixo (sem horário de verão desde 2019).
  08:00 BRT = 12:00 UTC | 07:00 BRT = 11:00 UTC | 21:00 BRT = 01:00 UTC (+1 dia)

Cada job itera sobre TODOS os tenants ativos do Core DB.
Falha em um tenant é capturada e logada — não aborta os demais.

SEPARAÇÃO DE AGENTES: estes jobs alimentam exclusivamente o Internal Agent
(médico/operação). Nunca enviar métricas operacionais para canal do paciente (agente).

ENVIO POR TENANT: os envios passam por channels.dispatcher.enviar_mensagem_tenant,
que resolve o canal (API oficial da Meta) e credenciais a partir do canal
ativo do tenant em canais_whatsapp — mesmo canal usado para o paciente. A
separação paciente/médico é garantida pelo DESTINO (medico_whatsapp), não por
um canal de envio diferente.

MODO DRY-RUN: passar dry_run=True aos jobs ou usar
  python -m agents.internal.scheduler --job all --dry-run
para validar resolução de número e contagem de tenants sem disparar WhatsApp real.

TOGGLES POR TENANT (padrão desligado): config["notificacoes"] controla quais jobs rodam para cada
tenant. Ausência de chave = ligado (DEFAULT ON). Exemplo:
  {"notificacoes": {"bom_dia": false, "resumo_noturno": true, "aniversarios": true, "nps": true}}

NPS — DELAY POR TENANT: config["nps"]["delay_horas"] (default 1) controla
quanto tempo após o atendimento (status_presenca='atendido') a pergunta de
NPS é disparada. Exemplo: {"nps": {"delay_horas": 3}}
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from channels.dispatcher import enviar_mensagem_tenant
from db.client import get_supabase
from db.privacy import communication_allowed, get_active_term
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

_BR_TZ = timezone(timedelta(hours=-4))


# ─── Helpers multi-tenant ─────────────────────────────────────────────────────


async def _buscar_tenants_ativos() -> list[dict]:
    """Retorna id, nome e config de todos os tenants ativos do Core DB."""
    result = get_supabase().table("tenants").select("id, nome, config").eq("ativo", True).execute()
    return result.data or []


def _notificacao_ativa(config: dict, nome: str) -> bool:
    """Verifica se a notificação 'nome' está ativa para este tenant.

    Só True explícito liga (padrão DESLIGADO desde 04/10/2026: clínica nova não
    recebe/dispara nada sem configurar; as existentes foram gravadas com o valor
    que tinham pela migration 20261005000002). Por tenant — um não afeta os
    demais. O alerta de erro (monitor) nunca passa por este check.
    """
    return ((config or {}).get("notificacoes") or {}).get(nome) is True


def _resolver_medico_whatsapp(tenant_id: str, config: dict) -> tuple[str | None, str]:
    """Resolve o número WhatsApp do médico e informa a origem.

    Número vem SOMENTE de config["medico_whatsapp"]. Sem fallback de env.
    Ausente → (None, "nenhum"). _enviar_ao_medico pula com log.warning.
    tenant_id mantido na assinatura para compatibilidade com chamadores e dry-run.
    """
    numero = config.get("medico_whatsapp")
    if numero:
        return numero, "config"
    return None, "nenhum"


async def _enviar_ao_medico(
    texto: str, tenant_id: str, config: dict, *, dry_run: bool = False
) -> bool:
    """Envia (ou simula) mensagem para o médico do tenant via WhatsApp.

    dry_run=True: loga o que enviaria e retorna True sem chamar enviar_mensagem_tenant.
    Retorna False se medico_whatsapp não estiver configurado para este tenant.
    """
    numero, origem = _resolver_medico_whatsapp(tenant_id, config)

    if not numero:
        log.warning(
            "medico_whatsapp não configurado — envio ignorado | tenant=%s | origem=%s",
            tenant_id,
            origem,
        )
        return False

    if dry_run:
        preview = texto[:80].replace("\n", " ")
        log.info(
            "[DRY-RUN] tenant=%s | numero=%s | origem=%s | msg=%dc | preview=%s",
            tenant_id,
            numero,
            origem,
            len(texto),
            preview,
        )
        return True

    return await enviar_mensagem_tenant(
        tenant_id=tenant_id,
        numero_destino=numero,
        texto=texto,
        core_db=get_supabase(),
    )


_TITULOS_POR_TIPO: dict[str, str] = {
    "agenda_diaria": "Resumo da agenda",
    "resumo_noturno": "Resumo do dia",
    "aniversarios": "Aniversários de amanhã",
}


def _montar_titulo(tipo: str) -> str:
    """Monta o título (coluna NOT NULL em crm_notificacoes) a partir do tipo.

    aniversarios refere-se ao dia seguinte (job avisa sobre amanhã); os
    demais tipos usam a data de hoje. Tipo desconhecido cai no próprio
    valor de `tipo` — nunca retorna vazio/None.
    """
    agora = datetime.now(_BR_TZ)
    dia = agora.date() + timedelta(days=1) if tipo == "aniversarios" else agora.date()
    base = _TITULOS_POR_TIPO.get(tipo, tipo)
    return f"{base} — {dia.strftime('%d/%m/%Y')}"


async def _registrar_notificacao(
    tenant_id: str,
    tipo: str,
    mensagem: str,
    contato_id: str | None = None,
    *,
    dry_run: bool = False,
) -> None:
    """Grava notificação no CRM do tenant (Vertical DB). Nunca propaga exceção.

    dry_run=True: pula o insert e loga — não polui crm_notificacoes com testes.
    """
    if dry_run:
        log.info("[DRY-RUN] crm_notificacoes ignorado | tenant=%s | tipo=%s", tenant_id, tipo)
        return
    try:
        payload: dict = {
            "tenant_id": tenant_id,
            "tipo": tipo,
            "titulo": _montar_titulo(tipo),
            "mensagem": mensagem,
        }
        if contato_id:
            payload["contato_id"] = contato_id
        get_vertical_supabase().table("crm_notificacoes").insert(payload).execute()
    except Exception:
        log.exception("Erro ao registrar crm_notificacao | tenant=%s | tipo=%s", tenant_id, tipo)


# ─── Job: bom_dia ─────────────────────────────────────────────────────────────


def _formatar_agenda(agenda: list[dict], hoje_str: str, nome_tenant: str = "") -> str:
    saudacao = f"Bom dia{', ' + nome_tenant if nome_tenant else ''}!"
    linhas = [
        f"☀️ {saudacao}\n",
        f"📅 *Agenda de hoje — {hoje_str}* | {len(agenda)} consulta(s)\n",
    ]
    for item in agenda:
        dt = datetime.fromisoformat(item["data_hora"].replace("Z", "+00:00")).astimezone(_BR_TZ)
        horario = dt.strftime("%Hh%M")
        nome = item.get("nome") or "Paciente sem nome"
        brief = item.get("brief") or {}
        novo = brief.get("tipo") == "novo"

        linha_nome = f"⏰ *{horario} — {nome}*"
        if novo:
            linha_nome += " ⭐ _Novo paciente_"
        linhas.append(linha_nome)

        if brief.get("resumo_clinico"):
            linhas.append(f"  🩺 {brief['resumo_clinico']}")
        if brief.get("pontos_pessoais"):
            pontos = " • ".join(brief["pontos_pessoais"][:3])
            linhas.append(f"  👤 {pontos}")
        if brief.get("o_que_reforcar_hoje"):
            linhas.append(f"  💡 {brief['o_que_reforcar_hoje'][0]}")
        if novo and brief.get("alerta_novo_paciente"):
            linhas.append(f"  ⭐ {brief['alerta_novo_paciente']}")
        if brief.get("pendencias"):
            linhas.append(f"  📌 {brief['pendencias'][0]}")
        linhas.append("")

    return "\n".join(linhas)


async def _bom_dia_tenant(
    tenant_id: str, tenant_nome: str, config: dict, *, dry_run: bool = False
) -> None:
    if not _notificacao_ativa(config, "bom_dia"):
        log.info("notificacao bom_dia desativada | tenant=%s", tenant_id)
        return

    from agents.internal.tools import get_agenda_do_dia

    hoje = datetime.now(_BR_TZ).date()
    hoje_str = hoje.strftime("%d/%m/%Y")
    agenda = await get_agenda_do_dia(tenant_id=tenant_id, data=hoje)

    if not agenda:
        mensagem = f"☀️ Bom dia!\n\nSem consultas agendadas para hoje, {hoje_str}. ✅"
    else:
        mensagem = _formatar_agenda(agenda, hoje_str, nome_tenant=tenant_nome)

    enviado = await _enviar_ao_medico(mensagem, tenant_id, config, dry_run=dry_run)
    if not enviado:
        log.error("_bom_dia_tenant: falha ao enviar | tenant=%s", tenant_id)

    await _registrar_notificacao(tenant_id, "agenda_diaria", mensagem, dry_run=dry_run)


async def job_bom_dia(*, dry_run: bool = False) -> None:
    """Envia agenda do dia para cada tenant ativo.

    Agendado: 12:00 UTC = 08:00 em UTC-4. Apenas dias úteis (seg–sex).
    dry_run=True: monta a mensagem e loga, não envia WhatsApp nem grava no banco.
    """
    modo = "[DRY-RUN] " if dry_run else ""
    log.info("%sJob bom_dia iniciado", modo)
    tenants = await _buscar_tenants_ativos()
    log.info("%sJob bom_dia | tenants_ativos=%d", modo, len(tenants))
    for t in tenants:
        try:
            await _bom_dia_tenant(
                t["id"], t.get("nome", ""), t.get("config") or {}, dry_run=dry_run
            )
        except Exception:
            log.exception("job_bom_dia falhou | tenant=%s | nome=%s", t["id"], t.get("nome"))


# ─── Job: resumo_noturno ──────────────────────────────────────────────────────


async def _resumo_noturno_tenant(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
    if not _notificacao_ativa(config, "resumo_noturno"):
        log.info("notificacao resumo_noturno desativada | tenant=%s", tenant_id)
        return

    vertical = get_vertical_supabase()

    hoje = datetime.now(_BR_TZ).date()
    inicio_dia = datetime(hoje.year, hoje.month, hoje.day, tzinfo=_BR_TZ)
    fim_dia = inicio_dia + timedelta(days=1)

    result_ag = (
        vertical.table("agendamentos")
        .select("id, data_hora, tipo, status")
        .eq("tenant_id", tenant_id)
        .gte("data_hora", inicio_dia.isoformat())
        .lt("data_hora", fim_dia.isoformat())
        .execute()
    )
    agendamentos_hoje = result_ag.data or []

    result_fu = (
        vertical.table("followup_queue")
        .select("contato_id, tipo, mensagem, agendado_para")
        .eq("tenant_id", tenant_id)
        .eq("status", "pendente")
        .limit(10)
        .execute()
    )
    followups = result_fu.data or []

    confirmados = sum(
        1 for a in agendamentos_hoje if a.get("status") in ("confirmado", "confirmado_paciente")
    )
    linhas = [
        f"🌙 *Resumo do dia — {hoje.strftime('%d/%m/%Y')}*\n",
        f"✅ Consultas realizadas: {confirmados}",
        f"📊 Total agendado: {len(agendamentos_hoje)}\n",
    ]

    if followups:
        linhas.append(f"📌 *Follow-ups pendentes ({len(followups)}):*")
        for fu in followups[:5]:
            descricao = fu.get("mensagem") or fu.get("tipo") or "—"
            agendado_para = fu.get("agendado_para")
            linha = f"  • {descricao}"
            if agendado_para:
                try:
                    dt_ag = datetime.fromisoformat(agendado_para.replace("Z", "+00:00")).astimezone(
                        _BR_TZ
                    )
                    linha += f" (agendado: {dt_ag.strftime('%d/%m %Hh%M')})"
                except (ValueError, TypeError):
                    linha += f" (agendado: {agendado_para})"
            linhas.append(linha)
        if len(followups) > 5:
            linhas.append(f"  ... e mais {len(followups) - 5}.")
    else:
        linhas.append("✅ Sem follow-ups pendentes.")

    mensagem = "\n".join(linhas)
    enviado = await _enviar_ao_medico(mensagem, tenant_id, config, dry_run=dry_run)
    if not enviado:
        log.error("_resumo_noturno_tenant: falha ao enviar | tenant=%s", tenant_id)


async def job_resumo_noturno(*, dry_run: bool = False) -> None:
    """Envia resumo do dia e follow-ups para cada tenant ativo.

    Agendado: 01:00 UTC = 21:00 em UTC-4.
    dry_run=True: monta o resumo e loga, não envia WhatsApp.
    """
    modo = "[DRY-RUN] " if dry_run else ""
    log.info("%sJob resumo_noturno iniciado", modo)
    tenants = await _buscar_tenants_ativos()
    log.info("%sJob resumo_noturno | tenants_ativos=%d", modo, len(tenants))
    for t in tenants:
        try:
            await _resumo_noturno_tenant(t["id"], t.get("config") or {}, dry_run=dry_run)
        except Exception:
            log.exception("job_resumo_noturno falhou | tenant=%s | nome=%s", t["id"], t.get("nome"))


# ─── Job: aniversarios ────────────────────────────────────────────────────────


def _aniversario_amanha(data_nasc_str: str | None, amanha_mes: int, amanha_dia: int) -> bool:
    if not data_nasc_str:
        return False
    try:
        from datetime import date

        dt = date.fromisoformat(data_nasc_str[:10])
        return dt.month == amanha_mes and dt.day == amanha_dia
    except (ValueError, TypeError):
        return False


async def _aniversarios_tenant(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
    if not _notificacao_ativa(config, "aniversarios"):
        log.info("notificacao aniversarios desativada | tenant=%s", tenant_id)
        return

    core = get_supabase()
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()

    result = (
        core.table("contatos")
        .select("id, nome, telefone, data_nascimento")
        .eq("tenant_id", tenant_id)
        .not_.is_("data_nascimento", "null")
        .execute()
    )
    contatos = result.data or []

    aniversariantes = [
        c
        for c in contatos
        if _aniversario_amanha(c.get("data_nascimento"), amanha.month, amanha.day)
    ]

    if not aniversariantes:
        log.info("Nenhum aniversariante amanhã | tenant=%s", tenant_id)
        return

    linhas = [f"🎂 *Aniversários amanhã ({amanha.strftime('%d/%m/%Y')}):*\n"]
    for c in aniversariantes:
        nome = c.get("nome") or "Paciente sem nome"
        tel = c.get("telefone") or "—"
        linhas.append(f"• {nome} | {tel}")
    linhas.append("\n💡 Considere uma mensagem personalizada pelo CRM.")

    mensagem = "\n".join(linhas)
    enviado = await _enviar_ao_medico(mensagem, tenant_id, config, dry_run=dry_run)
    if not enviado:
        log.error("_aniversarios_tenant: falha ao enviar | tenant=%s", tenant_id)


async def job_aniversarios(*, dry_run: bool = False) -> None:
    """Alerta sobre aniversários para cada tenant ativo.

    Agendado: 11:00 UTC = 07:00 em UTC-4. Diário.
    Supabase PostgREST não suporta DATE_PART; filtro feito em Python.
    dry_run=True: monta o alerta e loga, não envia WhatsApp.
    """
    modo = "[DRY-RUN] " if dry_run else ""
    log.info("%sJob aniversarios iniciado", modo)
    tenants = await _buscar_tenants_ativos()
    log.info("%sJob aniversarios | tenants_ativos=%d", modo, len(tenants))
    for t in tenants:
        try:
            await _aniversarios_tenant(t["id"], t.get("config") or {}, dry_run=dry_run)
        except Exception:
            log.exception("job_aniversarios falhou | tenant=%s | nome=%s", t["id"], t.get("nome"))


# ─── Job: disparar_nps ────────────────────────────────────────────────────────
# Gatilho: agendamentos.status_presenca='atendido' (eixo "o que aconteceu no
# dia", distinto de status="validade do agendamento"). atendido_em marca o
# momento exato. Roda a cada 60 min; margem de 2h na janela evita perder
# agendamentos se uma execução atrasar ou falhar.
#
# Envio é send-then-insert: a pergunta é enviada ANTES de criar o registro em
# nps_respostas. Se o envio falhar, nada é gravado — o próprio dedup por
# agendamento_id (passo seguinte) deixa o próximo ciclo tentar de novo, sem
# precisar de coluna extra para marcar "enviado". Risco residual aceito: se o
# insert falhar bem depois de um envio bem-sucedido (DB fora do ar
# independente da Meta), o próximo ciclo reenviaria a pergunta.

_JANELA_MARGEM_NPS = timedelta(hours=2)


def _buscar_telefone_contato(core, tenant_id: str, contato_id: str) -> str | None:
    result = (
        core.table("contatos")
        .select("telefone")
        .eq("id", contato_id)
        .eq("tenant_id", tenant_id)
        .limit(1)
        .execute()
    )
    return result.data[0]["telefone"] if result.data else None


def _montar_pergunta_nps(config: dict) -> str:
    nome_medico = (config.get("perfil_medico") or {}).get("nome") or "o médico"
    return (
        f"De 0 a 10, qual a chance de você recomendar {nome_medico} para um "
        "amigo ou familiar? Responda só com o número. 🙏"
    )


async def _disparar_nps_agendamento(
    core, vertical, tenant_id: str, config: dict, agendamento: dict, *, dry_run: bool = False
) -> None:
    agendamento_id = agendamento["id"]
    contato_id = agendamento.get("contato_id")
    if not contato_id:
        return

    existente = (
        vertical.table("nps_respostas")
        .select("id")
        .eq("agendamento_id", agendamento_id)
        .limit(1)
        .execute()
    )
    if existente.data:
        return

    termo = get_active_term(vertical, tenant_id)
    if not communication_allowed(
        vertical,
        tenant_id=tenant_id,
        contato_id=contato_id,
        channel="whatsapp",
        purpose="nps",
        current_term=termo,
    ):
        log.info(
            "NPS bloqueado por preferência LGPD | tenant=%s contato=%s",
            tenant_id,
            contato_id,
        )
        return

    telefone = _buscar_telefone_contato(core, tenant_id, contato_id)
    if not telefone:
        log.error(
            "NPS: contato sem telefone — disparo ignorado "
            "| tenant=%s | contato=%s | agendamento=%s",
            tenant_id,
            contato_id,
            agendamento_id,
        )
        return

    pergunta = _montar_pergunta_nps(config)

    if dry_run:
        log.info(
            "[DRY-RUN] NPS seria disparado | tenant=%s | agendamento=%s | telefone=%s",
            tenant_id,
            agendamento_id,
            telefone,
        )
        return

    enviado = await enviar_mensagem_tenant(
        tenant_id=tenant_id,
        numero_destino=telefone,
        texto=pergunta,
        core_db=core,
    )
    if not enviado:
        log.error(
            "NPS: falha ao enviar pergunta — registro NÃO criado, próximo ciclo tenta de novo "
            "| tenant=%s | agendamento=%s",
            tenant_id,
            agendamento_id,
        )
        return

    try:
        vertical.table("nps_respostas").insert(
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "agendamento_id": agendamento_id,
                "score": None,
            }
        ).execute()
    except Exception:
        log.exception(
            "NPS: pergunta enviada mas falha ao registrar nps_respostas pendente "
            "| tenant=%s | agendamento=%s",
            tenant_id,
            agendamento_id,
        )


async def _disparar_nps_tenant(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
    if not _notificacao_ativa(config, "nps"):
        log.info("notificacao nps desativada | tenant=%s", tenant_id)
        return

    delay_horas = (config.get("nps") or {}).get("delay_horas", 1)
    agora = datetime.now(timezone.utc)
    cutoff_fim = agora - timedelta(hours=delay_horas)
    cutoff_inicio = cutoff_fim - _JANELA_MARGEM_NPS

    vertical = get_vertical_supabase()
    core = get_supabase()

    result = (
        vertical.table("agendamentos")
        .select("id, contato_id")
        .eq("tenant_id", tenant_id)
        .eq("status_presenca", "atendido")
        .gte("atendido_em", cutoff_inicio.isoformat())
        .lte("atendido_em", cutoff_fim.isoformat())
        .execute()
    )
    candidatos = result.data or []

    for ag in candidatos:
        try:
            await _disparar_nps_agendamento(core, vertical, tenant_id, config, ag, dry_run=dry_run)
        except Exception:
            log.exception(
                "Erro ao disparar NPS | tenant=%s | agendamento=%s", tenant_id, ag.get("id")
            )


async def job_disparar_nps(*, dry_run: bool = False) -> None:
    """Dispara pedido de NPS para agendamentos atendidos há >= delay_horas (por tenant).

    Agendado: a cada 60 minutos.
    dry_run=True: detecta candidatos e loga, não envia WhatsApp nem grava no banco.
    """
    modo = "[DRY-RUN] " if dry_run else ""
    log.info("%sJob disparar_nps iniciado", modo)
    tenants = await _buscar_tenants_ativos()
    log.info("%sJob disparar_nps | tenants_ativos=%d", modo, len(tenants))
    for t in tenants:
        try:
            await _disparar_nps_tenant(t["id"], t.get("config") or {}, dry_run=dry_run)
        except Exception:
            log.exception("job_disparar_nps falhou | tenant=%s | nome=%s", t["id"], t.get("nome"))


# ─── Registro do scheduler (produção — inalterado) ────────────────────────────


def start_scheduler() -> AsyncIOScheduler:
    """Cria e retorna o scheduler do Internal Agent com os 4 jobs configurados.

    Horários UTC (UTC-4):
      bom_dia        → 12:00 UTC (08:00 BRT) — seg a sex
      aniversarios   → 11:00 UTC (07:00 BRT) — diário
      resumo_noturno → 01:00 UTC (21:00 BRT) — diário
      disparar_nps   → a cada 60 minutos — todos os dias

    Jobs chamados sem dry_run — dry_run=False é o padrão.
    Chamar scheduler.start() e armazenar a referência para shutdown.
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_bom_dia, "cron", hour=12, minute=0, day_of_week="mon-fri")
    scheduler.add_job(job_aniversarios, "cron", hour=11, minute=0)
    scheduler.add_job(job_resumo_noturno, "cron", hour=1, minute=0)
    scheduler.add_job(job_disparar_nps, "interval", minutes=60)
    return scheduler


# ─── Entrypoint manual ────────────────────────────────────────────────────────
# Uso: python -m agents.internal.scheduler --job all --dry-run
# Sem --dry-run, exige confirmação explícita no terminal antes de disparar.

if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Executa jobs do Internal Agent manualmente.")
    parser.add_argument(
        "--job",
        choices=["bom_dia", "resumo_noturno", "aniversarios", "disparar_nps", "all"],
        required=True,
        help="Job a executar.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula execução: resolve números e monta mensagens, mas NÃO envia WhatsApp nem "
        "grava no banco.",
    )
    args = parser.parse_args()

    if not args.dry_run:
        print(
            "\n⚠️  ATENÇÃO: modo LIVE — mensagens reais serão enviadas para o WhatsApp dos "
            "médicos.\n"
            f"   Job: {args.job}\n"
        )
        confirmacao = input("   Digite CONFIRMAR para continuar (ou qualquer coisa para abortar): ")
        if confirmacao.strip() != "CONFIRMAR":
            print("Abortado.")
            sys.exit(0)

    async def _main() -> None:
        coros = []
        if args.job in ("bom_dia", "all"):
            coros.append(job_bom_dia(dry_run=args.dry_run))
        if args.job in ("resumo_noturno", "all"):
            coros.append(job_resumo_noturno(dry_run=args.dry_run))
        if args.job in ("aniversarios", "all"):
            coros.append(job_aniversarios(dry_run=args.dry_run))
        if args.job in ("disparar_nps", "all"):
            coros.append(job_disparar_nps(dry_run=args.dry_run))
        for coro in coros:
            await coro

    asyncio.run(_main())
