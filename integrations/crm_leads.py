"""Notificações best-effort ao painel CRM (Vertical) sobre eventos de lead.

FIRE-AND-FORGET: estas chamadas NUNCA bloqueiam nem interrompem o fluxo de
atendimento no WhatsApp. Reaproveitam settings.crm_url + settings.internal_api_key
(mesma config/auth do callback de NPS — ver gateway/nps_handler.py). Se a chamada
falhar (timeout, rede, 4xx/5xx) ou não puder ser agendada, apenas loga; a resposta
ao paciente segue normalmente.
"""

import asyncio
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT_S = 3.0
_ENDPOINT_LINK_CONTATO = "/api/leads/link-contato"
_ENDPOINT_ADVANCE_STAGE = "/api/leads/advance-stage"

# Referência forte às tasks fire-and-forget para não serem coletadas pelo GC
# antes de concluírem (asyncio.create_task só mantém referência fraca).
_tasks: set[asyncio.Task] = set()


async def _post_crm(endpoint: str, payload: dict) -> None:
    """POST best-effort ao CRM. NUNCA levanta exceção — só loga em caso de falha."""
    crm_url = getattr(settings, "crm_url", None)
    if not crm_url:
        log.warning("[CRM-LEADS] crm_url não configurado — %s ignorado", endpoint)
        return

    url = crm_url.rstrip("/") + endpoint
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"x-internal-key": settings.internal_api_key or ""},
            )
        if response.status_code >= 400:
            log.warning(
                "[CRM-LEADS] %s falhou | status=%d | body=%s",
                endpoint,
                response.status_code,
                response.text[:200],
            )
    except Exception:
        log.warning("[CRM-LEADS] erro ao chamar %s (best-effort, ignorado)", url, exc_info=True)


def _disparar(coro) -> None:
    """Agenda a coroutine fire-and-forget. NUNCA bloqueia nem levanta: se não
    houver event loop em execução (ou qualquer falha ao agendar), fecha a
    coroutine e loga, sem propagar."""
    try:
        task = asyncio.create_task(coro)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception:
        try:
            coro.close()
        except Exception:
            pass
        log.warning("[CRM-LEADS] não foi possível agendar notificação (ignorado)", exc_info=True)


def notificar_contato_vinculado(
    tenant_id: str, contato_id: str, telefone: str, nome: str | None
) -> None:
    """Fire-and-forget: avisa o CRM que um contato NOVO foi criado no Core, para
    ele tentar vincular a um lead existente. Best-effort; nunca bloqueia/quebra."""
    _disparar(
        _post_crm(
            _ENDPOINT_LINK_CONTATO,
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "telefone": telefone,
                "nome": nome,
            },
        )
    )


def notificar_estagio_avancado(tenant_id: str, contato_id: str) -> None:
    """Fire-and-forget: avisa o CRM que um contato virou paciente (agendamento
    confirmado), para avançar o estágio do lead. Best-effort; nunca bloqueia."""
    _disparar(
        _post_crm(
            _ENDPOINT_ADVANCE_STAGE,
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
            },
        )
    )
