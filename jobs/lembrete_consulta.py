import asyncio
import logging
from datetime import datetime, timedelta, timezone

from channels.dispatcher import enviar_mensagem_tenant, enviar_template_tenant
from channels.templates import (
    PAYLOAD_CONFIRMAR,
    PAYLOAD_REMARCAR,
    TemplateMeta,
    dia_da_semana,
    parametro,
    template_da_clinica,
)
from core.fuso import FUSO_PADRAO_HORAS, fuso_da_config
from db.client import get_supabase
from db.privacy import communication_allowed, get_active_term
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

# Configuração por clínica (tenants.config no CORE):
#   notificacoes.lembrete       bool  — liga o lembrete D-1. PADRÃO DESLIGADO:
#                                       só envia quem ligou explicitamente.
#   lembrete.mensagem           str   — modelo com {primeiro_nome} e {horario}.
#   fuso_utc_horas              int   — fuso da clínica (core/fuso.py; padrão -3,
#                                       Brasília; aceita o legado
#                                       lembrete.utc_offset_horas).
#   templates.lembrete_consulta       — template aprovado na Meta (item 3.1). Com
#                                       ele, o lembrete chega mesmo fora da janela
#                                       de 24 h e traz os botões Confirmar /
#                                       Preciso remarcar. Sem ele, texto livre.

_MENSAGEM_PADRAO = (
    "Olá, {primeiro_nome}! 😊\n\n"
    "Lembrando que você tem consulta *amanhã* às *{horario}*.\n\n"
    "Você confirma sua presença?\n"
    "✅ Responda *SIM* para confirmar\n"
    "🔁 Responda *REMARCAR* se precisar mudar o horário"
)

# Pausa entre lembretes de um mesmo lote (segundos). Este job dispara N
# lembretes em sequência; sem espaçamento vira uma rajada — principal gatilho
# de bloqueio pela Meta.
_PAUSA_ENTRE_LEMBRETES_S = 2.0


def _lembrete_habilitado(config: dict | None) -> bool:
    """Só True explícito liga o lembrete (padrão desligado)."""
    return ((config or {}).get("notificacoes") or {}).get("lembrete") is True


def _fuso(config: dict | None) -> timezone:
    return fuso_da_config(config)


def _modelo_mensagem(config: dict | None) -> str:
    modelo = ((config or {}).get("lembrete") or {}).get("mensagem")
    return modelo if isinstance(modelo, str) and modelo.strip() else _MENSAGEM_PADRAO


def _calcular_janela_amanha(
    fuso: timezone = timezone(timedelta(hours=FUSO_PADRAO_HORAS)),
) -> tuple[str, str]:
    """Retorna (inicio_utc_iso, fim_utc_iso) do dia seguinte no fuso da clínica."""
    amanha = datetime.now(tz=fuso) + timedelta(days=1)
    inicio = amanha.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    fim = amanha.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc)
    return inicio.isoformat(), fim.isoformat()


def _formatar_mensagem(nome: str, horario: str, modelo: str | None = None) -> str:
    primeiro_nome = (nome.strip() or "paciente").split()[0]
    try:
        return (modelo or _MENSAGEM_PADRAO).format(primeiro_nome=primeiro_nome, horario=horario)
    except (KeyError, IndexError, ValueError):
        # Modelo configurado com chave desconhecida: não deixa o paciente sem
        # lembrete nem manda texto quebrado — cai no padrão.
        log.warning("Modelo de lembrete inválido na config da clínica — usando o padrão")
        return _MENSAGEM_PADRAO.format(primeiro_nome=primeiro_nome, horario=horario)


def _tenants_com_lembrete(core) -> dict[str, dict]:
    """Clínicas ativas com o lembrete ligado → {tenant_id: config (+ _nome)}."""
    result = core.table("tenants").select("id, nome, config").eq("ativo", True).execute()
    return {
        t["id"]: {**(t.get("config") or {}), "_nome": t.get("nome") or ""}
        for t in (result.data or [])
        if _lembrete_habilitado(t.get("config"))
    }


def _nome_da_clinica(vertical, tenant_id: str, nome_tenant: str) -> str:
    """Nome do cabeçalho dos documentos (CRM → Meus modelos), senão o do tenant."""
    try:
        result = (
            vertical.table("tenant_config")
            .select("valor")
            .eq("tenant_id", tenant_id)
            .eq("chave", "documentos")
            .limit(1)
            .execute()
        )
        valor = (result.data[0].get("valor") if result.data else None) or {}
        nome = valor.get("clinica_nome") if isinstance(valor, dict) else None
        if isinstance(nome, str) and nome.strip():
            return nome.strip()
    except Exception:
        log.warning("Não foi possível ler o nome da clínica | tenant=%s", tenant_id)
    return nome_tenant.strip() or "nossa clínica"


def parametros_do_template(nome: str, clinica: str, dt_local: datetime) -> list[str]:
    """{{1}} primeiro nome, {{2}} clínica, {{3}} dia (quinta-feira, 30/10), {{4}} horário."""
    primeiro_nome = (nome.strip() or "paciente").split()[0]
    dia = f"{dia_da_semana(dt_local.isoweekday())}, {dt_local.strftime('%d/%m')}"
    return [
        parametro(primeiro_nome),
        parametro(clinica),
        parametro(dia),
        parametro(dt_local.strftime("%Hh%M")),
    ]


async def enviar_lembretes_do_dia() -> None:
    """Busca consultas de amanhã com lembrete pendente e envia via WhatsApp.

    Chamado pelo scheduler todo dia às 12:00 UTC (09:00 Brasília / 08:00 em UTC-4).
    Idempotente: filtra por lembrete_enviado=False, então re-execuções seguras.
    """
    log.info("Iniciando envio de lembretes de consulta...")

    core = get_supabase()
    tenants = _tenants_com_lembrete(core)
    if not tenants:
        log.info("Nenhuma clínica com lembrete ligado.")
        return

    vertical = get_vertical_supabase()
    for tenant_id, config in tenants.items():
        await _enviar_lembretes_tenant(core, vertical, tenant_id, config)


async def _enviar_lembretes_tenant(core, vertical, tenant_id: str, config: dict) -> None:
    fuso = _fuso(config)
    modelo = _modelo_mensagem(config)
    template: TemplateMeta | None = template_da_clinica(config, "lembrete_consulta")
    clinica = _nome_da_clinica(vertical, tenant_id, config.get("_nome") or "") if template else ""
    inicio_iso, fim_iso = _calcular_janela_amanha(fuso)

    result = (
        vertical.table("agendamentos")
        .select("id, data_hora, contato_id, convenio, tenant_id")
        .eq("status", "confirmado")
        .eq("lembrete_enviado", False)
        .eq("tenant_id", tenant_id)
        .gte("data_hora", inicio_iso)
        .lte("data_hora", fim_iso)
        .execute()
    )

    if not result.data:
        log.info("Nenhum agendamento para lembrete amanhã | tenant=%s", tenant_id)
        return

    log.info("Agendamentos para lembrete: %d | tenant=%s", len(result.data), tenant_id)

    enviados = 0
    falhas = 0

    for indice, ag in enumerate(result.data):
        if indice > 0:
            # Espaça os disparos do lote (anti-burst / anti-ban).
            await asyncio.sleep(_PAUSA_ENTRE_LEMBRETES_S)
        try:
            contato_result = (
                core.table("contatos")
                .select("nome, telefone, metadata")
                .eq("id", ag["contato_id"])
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )

            contato = contato_result.data[0] if contato_result.data else {}
            termo = get_active_term(vertical, ag["tenant_id"])
            if not communication_allowed(
                vertical,
                tenant_id=ag["tenant_id"],
                contato_id=ag["contato_id"],
                channel="whatsapp",
                purpose="lembretes",
                current_term=termo,
                transactional_fallback=True,
            ):
                log.info(
                    "Lembrete bloqueado por preferência LGPD | tenant=%s contato=%s",
                    ag["tenant_id"],
                    ag["contato_id"],
                )
                continue
            telefone = contato.get("telefone")
            # Paciente terceiro tem telefone sintético ("sem-telefone:..."); o
            # lembrete vai para o WhatsApp de quem contatou (responsável), salvo
            # em contatos.metadata.responsavel_telefone.
            if not telefone or telefone.startswith("sem-telefone:"):
                telefone = (contato.get("metadata") or {}).get("responsavel_telefone")

            if not telefone:
                log.warning(
                    "Contato sem telefone (nem responsável) — lembrete pulado | agendamento=%s |"
                    " contato_id=%s",
                    ag["id"],
                    ag["contato_id"],
                )
                falhas += 1
                continue

            dt = datetime.fromisoformat(ag["data_hora"].replace("Z", "+00:00"))
            dt_local = dt.astimezone(fuso)

            if template:
                enviado = await enviar_template_tenant(
                    tenant_id=ag["tenant_id"],
                    numero_destino=telefone,
                    nome=template.nome,
                    idioma=template.idioma,
                    parametros=parametros_do_template(contato.get("nome") or "", clinica, dt_local),
                    core_db=core,
                    payloads_botoes=[
                        f"{PAYLOAD_CONFIRMAR}{ag['id']}",
                        f"{PAYLOAD_REMARCAR}{ag['id']}",
                    ],
                )
            else:
                mensagem = _formatar_mensagem(
                    contato.get("nome") or "", dt_local.strftime("%Hh%M"), modelo
                )
                enviado = await enviar_mensagem_tenant(
                    tenant_id=ag["tenant_id"],
                    numero_destino=telefone,
                    texto=mensagem,
                    core_db=core,
                )

            if enviado:
                vertical.table("agendamentos").update(
                    {
                        "lembrete_enviado": True,
                        "lembrete_enviado_em": datetime.now(timezone.utc).isoformat(),
                    }
                ).eq("id", ag["id"]).execute()
                enviados += 1
                log.info(
                    "Lembrete enviado | agendamento=%s | telefone=%s",
                    ag["id"],
                    telefone,
                )
            else:
                falhas += 1
                log.warning("Falha no envio do lembrete | agendamento=%s", ag["id"])

        except Exception:
            falhas += 1
            log.exception("Erro ao processar lembrete | agendamento=%s", ag.get("id"))

    log.info(
        "Lembretes concluídos | tenant=%s | enviados=%d | falhas=%d", tenant_id, enviados, falhas
    )
