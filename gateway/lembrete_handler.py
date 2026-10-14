"""Detecção e tratamento das respostas a lembretes de consulta.

Itens 3.2 e 3.3 (04/10/2026):
- botão "Confirmar presença" do template, ou SIM por texto → confirma;
- botão "Preciso remarcar", ou NÃO / REMARCAR por texto → passa para a
  secretária (handoff), e a consulta NÃO é cancelada sozinha. Cancelar é ação
  irreversível (CLAUDE.md §4.8): quem decide é a clínica com o paciente.

Isolado do webhook principal para manter `webhook_cloudapi.py` legível.
Retorna a resposta pronta (str) quando a mensagem é detectada como resposta a
um lembrete; retorna None quando não é, sinalizando que o LangGraph deve atuar.
"""

import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from channels.base import MensagemRecebida
from channels.templates import PAYLOAD_CONFIRMAR, PAYLOAD_REMARCAR, dia_da_semana
from core.fuso import fuso_do_tenant
from db.client import get_supabase
from db.vertical_client import get_vertical_supabase
from gateway.tenant_resolver import TenantInfo

log = logging.getLogger(__name__)


_RESPOSTAS_SIM = {
    "sim",
    "s",
    "confirmo",
    "confirmado",
    "confirmar",
    "yes",
    "ok",
    "claro",
    "com certeza",
    "pode ser",
}
# NÃO e "cancelar" também viram pedido de remarcação: o paciente fala com a
# secretária, e a consulta só é cancelada por ela.
_RESPOSTAS_REMARCAR = {
    "nao",
    "n",
    "cancelar",
    "cancela",
    "cancelado",
    "no",
    "remarcar",
    "remarca",
    "reagendar",
    "desmarcar",
}
# Usado na fase "aguardando_remarcacao" para detectar recusa de remarcar
_RESPOSTAS_NAO_REMARCAR = {
    "nao",
    "n",
    "no",
    "depois",
    "obrigado",
    "obrigada",
    "ok",
    "tudo bem",
    "tudo certo",
    "nao precisa",
    "nao quero",
}


# ─── Helpers internos ─────────────────────────────────────────────────────────


def _normalizar(texto: str) -> str:
    """Remove acentos, lowercase e strip."""
    s = unicodedata.normalize("NFD", texto.strip().lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _buscar_agendamento_recente_lembrete(
    vertical,
    tenant_id: str,
    contato_id: str,
) -> dict | None:
    """Busca o agendamento com lembrete enviado nas últimas 24 h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    result = (
        vertical.table("agendamentos")
        .select("id, data_hora, convenio, status")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .eq("lembrete_enviado", True)
        .gte("lembrete_enviado_em", cutoff)
        .order("lembrete_enviado_em", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _buscar_metadata_conversa(core, conversa_id: str) -> dict:
    result = core.table("conversas").select("metadata").eq("id", conversa_id).limit(1).execute()
    if not result.data:
        return {}
    return result.data[0].get("metadata") or {}


def _salvar_metadata_conversa(
    core,
    tenant_id: str,
    conversa_id: str,
    metadata: dict,
) -> None:
    (
        core.table("conversas")
        .update({"metadata": metadata})
        .eq("id", conversa_id)
        .eq("tenant_id", tenant_id)
        .execute()
    )


def _buscar_agendamento_do_botao(
    vertical,
    tenant_id: str,
    contato_id: str,
    agendamento_id: str,
) -> dict | None:
    """Consulta do payload do botão, só se for desta clínica e deste paciente."""
    result = (
        vertical.table("agendamentos")
        .select("id, data_hora, convenio, status")
        .eq("id", agendamento_id)
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _fuso_da_clinica(tenant_id: str) -> timezone:
    """Fuso da clínica (tenants.config.fuso_utc_horas, padrão Brasília)."""
    return fuso_do_tenant(tenant_id)


def _quando(data_hora: str, fuso: timezone) -> str:
    """'quinta-feira, 30/10 às 14h00' no fuso da clínica."""
    dt = datetime.fromisoformat(str(data_hora).replace("Z", "+00:00")).astimezone(fuso)
    return f"{dia_da_semana(dt.isoweekday())}, {dt.strftime('%d/%m')} às {dt.strftime('%Hh%M')}"


def _primeiro_nome(mensagem: MensagemRecebida) -> str:
    nome = (mensagem.nome_remetente or "").strip()
    return nome.split()[0] if nome else "tudo bem"


async def _confirmar(vertical, tenant_id: str, ag: dict, mensagem: MensagemRecebida) -> str:
    try:
        (
            vertical.table("agendamentos")
            .update({"status": "confirmado_paciente"})
            .eq("id", ag["id"])
            .eq("tenant_id", tenant_id)
            .execute()
        )
    except Exception:
        log.exception("Erro ao confirmar agendamento | ag_id=%s", ag["id"])
    return f"Ótimo, {_primeiro_nome(mensagem)}! ✅ Sua consulta está confirmada.\n\nAté amanhã! 🤍"


async def _pedir_remarcacao(tenant_id: str, contato_id: str, ag: dict) -> str:
    """Handoff para a secretária com o pedido pronto; a consulta continua marcada."""
    from tools.verticals.clinica.handoff import _make_transferir_para_atendente

    quando = _quando(ag["data_hora"], _fuso_da_clinica(tenant_id))
    tool_fn = _make_transferir_para_atendente(get_vertical_supabase())(tenant_id)
    try:
        resposta_handoff = await tool_fn.ainvoke(
            {
                "contato_id": contato_id,
                "role_destino": "atendente",
                "motivo": f"Remarcar consulta de {quando}",
                "resumo": (
                    f"O paciente respondeu ao lembrete pedindo para remarcar a consulta de "
                    f"{quando}. A consulta continua marcada: remarque ou cancele na Agenda "
                    "depois de combinar o novo horário."
                ),
            }
        )
    except Exception:
        log.exception("Erro ao pedir remarcação | ag_id=%s", ag["id"])
        resposta_handoff = "Nossa equipe vai falar com você por aqui para combinar."
    return (
        f"Sem problema! Sua consulta de {quando} continua reservada até combinarmos "
        f"o novo horário.\n\n{resposta_handoff}"
    )


# ─── Ponto de entrada público ─────────────────────────────────────────────────


async def handle_resposta_lembrete(
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    contato_id: str | None,
    conversa_id: str | None,
) -> str | None:
    """Verifica se a mensagem é resposta a um lembrete.

    Retorna a resposta pronta quando detectado; None quando não é resposta a
    lembrete — o webhook deve prosseguir com o LangGraph normalmente.
    """
    if not contato_id or not conversa_id:
        return None

    texto = _normalizar(mensagem.texto or "")
    if not texto:
        return None

    core = get_supabase()
    vertical = get_vertical_supabase()

    # ── Fase 1: aguardando escolha de remarcação ──────────────────────────────
    metadata = _buscar_metadata_conversa(core, conversa_id)
    if metadata.get("aguardando_remarcacao") and metadata.get("agendamento_id"):
        palavras = set(texto.split())
        is_recusa = palavras & _RESPOSTAS_NAO_REMARCAR or texto in _RESPOSTAS_NAO_REMARCAR

        # Em qualquer caso, limpar o estado de aguardo
        _salvar_metadata_conversa(core, tenant.tenant_id, conversa_id, {})

        if is_recusa:
            return "Entendido! Fico à disposição sempre que precisar agendar. Até logo! 👋"

        # Paciente quer remarcar — passa ao LangGraph para processar a escolha
        return None

    # ── Botão do template (payload com o id da consulta) ──────────────────────
    button_id = (mensagem.metadata or {}).get("button_id") or ""
    for prefixo, acao in ((PAYLOAD_CONFIRMAR, "confirmar"), (PAYLOAD_REMARCAR, "remarcar")):
        if button_id.startswith(prefixo):
            ag = _buscar_agendamento_do_botao(
                vertical, tenant.tenant_id, contato_id, button_id[len(prefixo) :]
            )
            if ag is None or str(ag.get("status") or "").startswith("cancelado"):
                return (
                    "Não encontrei essa consulta marcada. "
                    "Me conta como posso te ajudar por aqui? 😊"
                )
            if acao == "confirmar":
                return await _confirmar(vertical, tenant.tenant_id, ag, mensagem)
            return await _pedir_remarcacao(tenant.tenant_id, contato_id, ag)

    # ── Texto livre: só vale com lembrete enviado nas últimas 24 h ────────────
    ag = _buscar_agendamento_recente_lembrete(vertical, tenant.tenant_id, contato_id)
    if ag is None:
        return None

    palavras = set(texto.split())
    if palavras & _RESPOSTAS_SIM or texto in _RESPOSTAS_SIM:
        return await _confirmar(vertical, tenant.tenant_id, ag, mensagem)
    if palavras & _RESPOSTAS_REMARCAR or texto in _RESPOSTAS_REMARCAR:
        return await _pedir_remarcacao(tenant.tenant_id, contato_id, ag)

    return None
