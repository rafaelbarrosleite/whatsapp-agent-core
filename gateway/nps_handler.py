"""Detecção e tratamento de respostas NPS (0-10) de pacientes via WhatsApp.

Isolado do webhook principal para manter `webhook_cloudapi.py` legível.
Retorna a mensagem de agradecimento (str) quando a mensagem é detectada como
resposta NPS válida e há registro pendente; retorna None caso contrário,
sinalizando que o LangGraph deve atuar normalmente.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from channels.base import MensagemRecebida
from db.vertical_client import get_vertical_supabase
from gateway.tenant_resolver import TenantInfo

log = logging.getLogger(__name__)

_AGRADECIMENTO: dict[str, str] = {
    "promotor": "Que ótimo! Fico feliz que tenha gostado. Até a próxima consulta! 😊",
    "neutro": "Obrigado pelo feedback! Se tiver alguma sugestão, é só nos dizer. 🙏",
    "detrator": "Obrigado por responder. Seu feedback é muito importante e vamos trabalhar para "
    "melhorar. Em breve entraremos em contato. 💙",
}

_CRM_NPS_URL = "/api/nps/resposta"


def _parsear_score(texto: str) -> int | None:
    """Retorna 0-10 se o texto for exclusivamente um número nesse intervalo; None caso contrário."""
    stripped = texto.strip()
    if not stripped.isdigit():
        return None
    num = int(stripped)
    if num < 0 or num > 10:
        return None
    return num


def _classificar(score: int) -> str:
    if score >= 9:
        return "promotor"
    if score >= 7:
        return "neutro"
    return "detrator"


def _buscar_nps_pendente(vertical, tenant_id: str, contato_id: str) -> dict | None:
    """Busca nps_respostas com score null nas últimas 24 horas para o contato."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    result = (
        vertical.table("nps_respostas")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .is_("score", "null")
        .gte("criado_em", cutoff)
        .order("criado_em", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        log.info(
            "[NPS] Nenhuma resposta NPS pendente encontrada | tenant=%s | contato=%s",
            tenant_id,
            contato_id,
        )
        return None
    return result.data[0]


async def _registrar_resposta(
    telefone: str, mensagem: str, tenant_id: str, contato_id: str
) -> None:
    """Chama POST /api/nps/resposta no CRM para persistir score e marcar detratores.

    Manda a identidade completa (tenant + contato): o telefone sozinho não é
    chave — o mesmo número pode ser paciente de duas clínicas.
    """
    crm_url = getattr(settings, "crm_url", None)
    if not crm_url:
        log.error(
            "[NPS] crm_url não configurado — score NUNCA será persistido via API. Configure "
            "CRM_URL."
        )
        return
    url = crm_url.rstrip("/") + _CRM_NPS_URL

    if not settings.internal_api_key:
        log.error("[NPS] internal_api_key não configurado — POST para %s será rejeitado (401)", url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "telefone": telefone,
                    "mensagem": mensagem,
                    "tenant_id": tenant_id,
                    "contato_id": contato_id,
                },
                headers={"x-internal-key": settings.internal_api_key or ""},
            )
        if response.status_code == 401:
            log.error(
                "[NPS] Falha ao registrar resposta | status=401 | body=%s | "
                "verifique INTERNAL_API_KEY",
                response.text[:200],
            )
        elif response.status_code != 200:
            log.error(
                "[NPS] Falha ao registrar resposta | status=%d | body=%s",
                response.status_code,
                response.text[:200],
            )
    except Exception:
        log.exception("[NPS] Erro ao chamar %s", url)


async def handle_resposta_nps(
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    contato_id: str | None,
) -> str | None:
    """Verifica se a mensagem é resposta NPS (número 0-10) com registro pendente.

    Retorna mensagem de agradecimento quando detectado; None quando não é
    resposta NPS — o webhook deve prosseguir com o LangGraph normalmente.
    """
    if not contato_id:
        return None

    score = _parsear_score(mensagem.texto or "")
    if score is None:
        return None

    vertical = get_vertical_supabase()
    pendente = _buscar_nps_pendente(vertical, tenant.tenant_id, contato_id)
    if pendente is None:
        return None

    await _registrar_resposta(
        mensagem.numero_remetente, mensagem.texto or "", tenant.tenant_id, contato_id
    )

    classificacao = _classificar(score)
    log.info(
        "[NPS] Resposta registrada | tenant=%s | contato=%s | score=%d | classificacao=%s",
        tenant.tenant_id,
        contato_id,
        score,
        classificacao,
    )

    return _AGRADECIMENTO[classificacao]
