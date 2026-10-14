"""jobs/reengajamento.py — reengajamento de leads que sumiram.

Muitos leads mandam a 1ª mensagem, o agente responde e eles somem. Este job varre
as conversas em que o agente está ativo (estado='aberta', agente_ativo=True), a
ÚLTIMA mensagem é do agente e não há resposta do paciente há ≥30min — e ainda
NÃO foram transferidas para atendente. Envia UMA mensagem amigável
(reengajamento.mensagem da clínica, ou o texto padrão). One-shot por conversa:
grava conversas.metadata.reengajado_em para nunca repetir.

Roda de 5 em 5 minutos (scheduler agente em app/main.py). Multi-tenant: itera os
tenants ativos; toggle por tenant em config["notificacoes"]["reengajamento"]
(padrão DESLIGADO). Janela de horário: só envia entre 08h e 20h no fuso da
clínica (lembrete.utc_offset_horas) para não incomodar o paciente de madrugada.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from channels.dispatcher import enviar_mensagem_tenant
from core.mensagem_prefixo import prefixar_mensagem_agente
from db.client import get_supabase
from db.conversas import salvar_mensagem
from db.privacy import communication_allowed, get_active_term
from db.vertical_client import get_vertical_supabase
from jobs.lembrete_consulta import _fuso

log = logging.getLogger(__name__)

_INATIVIDADE_MIN = 30  # silêncio mínimo do paciente para reengajar
_JANELA_HORAS = 48  # não reengaja conversas mais velhas que isso
_PAUSA_ENTRE_ENVIOS_S = 2.0  # anti-burst (mesmo racional do lembrete)
_HORA_INICIO, _HORA_FIM = 8, 20  # janela de envio, no fuso da clínica

# Texto padrão, sem nome de médico: cada clínica pode trocar em
# tenants.config.reengajamento.mensagem.
_MENSAGEM_PADRAO = (
    "Oi! 😊 Vi que nossa conversa ficou por aqui e fiquei na dúvida se você "
    "ainda precisa de ajuda.\n\n"
    "Posso te dar mais informações, ajudar a agendar sua consulta ou te "
    "conectar com alguém da nossa equipe. É só me chamar por aqui! 💚"
)


def _reengajamento_ativo(config: dict) -> bool:
    """Só True explícito liga (padrão desligado desde 04/10/2026: clínica nova
    não dispara nada sem ter configurado; as existentes foram gravadas com o
    valor que tinham pela migration 20261005000002)."""
    return ((config or {}).get("notificacoes") or {}).get("reengajamento") is True


def _mensagem(config: dict) -> str:
    texto = ((config or {}).get("reengajamento") or {}).get("mensagem")
    return texto.strip() if isinstance(texto, str) and texto.strip() else _MENSAGEM_PADRAO


def _dentro_da_janela(agora: datetime, config: dict) -> bool:
    """Entre 08h e 20h no fuso da clínica (lembrete.utc_offset_horas)."""
    hora_local = agora.astimezone(_fuso(config)).hour
    return _HORA_INICIO <= hora_local < _HORA_FIM


async def _buscar_tenants_ativos(core) -> dict[str, dict]:
    result = core.table("tenants").select("id, config").eq("ativo", True).execute()
    return {t["id"]: (t.get("config") or {}) for t in (result.data or [])}


async def _tem_handoff_ativo(vertical, conversa_id: str, tenant_id: str) -> bool:
    res = (
        vertical.table("handoff_fila")
        .select("id")
        .eq("conversa_id", conversa_id)
        .eq("tenant_id", tenant_id)
        .in_("status", ["aguardando", "em_atendimento"])
        .limit(1)
        .execute()
    )
    return bool(res.data)


async def _ultima_mensagem_eh_agente(core, conversa_id: str) -> bool:
    res = (
        core.table("mensagens")
        .select("papel")
        .eq("conversa_id", conversa_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return bool(res.data) and res.data[0].get("papel") == "agente"


async def reengajar_conversas_paradas(*, dry_run: bool = False) -> None:
    """Envia (ou simula) a mensagem de reengajamento para leads parados ≥30min."""
    agora = datetime.now(timezone.utc)
    core = get_supabase()
    vertical = get_vertical_supabase()

    tenants = {
        tid: cfg
        for tid, cfg in (await _buscar_tenants_ativos(core)).items()
        if _reengajamento_ativo(cfg) and _dentro_da_janela(agora, cfg)
    }
    if not tenants:
        return

    limite_inativo = (agora - timedelta(minutes=_INATIVIDADE_MIN)).isoformat()
    limite_janela = (agora - timedelta(hours=_JANELA_HORAS)).isoformat()

    convs = (
        core.table("conversas")
        .select("id, tenant_id, contato_id, metadata, updated_at")
        .eq("estado", "aberta")
        .eq("agente_ativo", True)
        .lte("updated_at", limite_inativo)
        .gte("updated_at", limite_janela)
        .in_("tenant_id", list(tenants.keys()))
        .order("updated_at", desc=True)
        .limit(200)
        .execute()
    )
    if not convs.data:
        return

    enviados = 0
    for conv in convs.data:
        tenant_id = conv["tenant_id"]
        config = tenants.get(tenant_id, {})
        if not _reengajamento_ativo(config):
            continue

        metadata = conv.get("metadata") or {}
        if metadata.get("reengajado_em"):
            continue  # one-shot: já reengajamos esta conversa

        try:
            if await _tem_handoff_ativo(vertical, conv["id"], tenant_id):
                continue  # já foi transferido a um humano — não reengaja
            if not await _ultima_mensagem_eh_agente(core, conv["id"]):
                continue  # última fala não é do agente (paciente respondeu)

            termo = get_active_term(vertical, tenant_id)
            if not communication_allowed(
                vertical,
                tenant_id=tenant_id,
                contato_id=conv["contato_id"],
                channel="whatsapp",
                purpose="followup_comercial",
                current_term=termo,
            ):
                log.info(
                    "Reengajamento bloqueado por preferência LGPD | tenant=%s contato=%s",
                    tenant_id,
                    conv["contato_id"],
                )
                continue

            contato_res = (
                core.table("contatos")
                .select("telefone")
                .eq("id", conv["contato_id"])
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            telefone = contato_res.data[0].get("telefone") if contato_res.data else None
            if not telefone:
                continue

            texto = prefixar_mensagem_agente(_mensagem(config), config.get("nome_agente"))

            if dry_run:
                log.info(
                    "[DRY-RUN] reengajaria | tenant=%s | conversa=%s | tel=%s",
                    tenant_id,
                    conv["id"],
                    telefone,
                )
                continue

            if enviados > 0:
                await asyncio.sleep(_PAUSA_ENTRE_ENVIOS_S)

            enviado = await enviar_mensagem_tenant(
                tenant_id=tenant_id,
                numero_destino=telefone,
                texto=texto,
                core_db=core,
            )
            if not enviado:
                log.warning("Reengajamento não enviado | conversa=%s", conv["id"])
                continue

            # Persiste na conversa e marca o one-shot (nunca reenvia).
            try:
                salvar_mensagem(core, tenant_id, conv["id"], "agente", texto)
            except Exception:
                log.error(
                    "Reengajamento enviado mas falhou ao persistir | conversa=%s",
                    conv["id"],
                    exc_info=True,
                )
            core.table("conversas").update(
                {
                    "metadata": {**metadata, "reengajado_em": agora.isoformat()},
                }
            ).eq("id", conv["id"]).eq("tenant_id", tenant_id).execute()

            enviados += 1
            log.info("Reengajamento enviado | tenant=%s | conversa=%s", tenant_id, conv["id"])
        except Exception:
            log.exception("Erro ao reengajar conversa=%s", conv.get("id"))

    log.info("Reengajamento concluído | enviados=%d", enviados)
