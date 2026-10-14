"""Integração com Google Calendar para criação automática de eventos de consulta."""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from db.vertical_client import get_vertical_supabase
from security.cifra_tokens import cifrar, decifrar, esta_cifrado

log = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
PROVEDOR = "google_calendar"


async def _get_valid_access_token(tenant_id: str) -> tuple[str, str] | None:
    """Retorna (access_token, calendar_id) válido para o tenant.

    Faz refresh automático se o token estiver a menos de 5 minutos do vencimento.
    Retorna None se não há integração ativa para o tenant.
    """
    vertical = get_vertical_supabase()

    result = (
        vertical.table("integracoes_oauth")
        .select("access_token, refresh_token, token_expira_em, calendar_id")
        .eq("tenant_id", tenant_id)
        .eq("provedor", "google_calendar")
        .eq("ativo", True)
        .single()
        .execute()
    )

    if not result.data:
        return None

    data = result.data
    chave = settings.tokens_crypto_key
    access_token = decifrar(data["access_token"], tenant_id, PROVEDOR, chave)
    refresh_token = decifrar(data["refresh_token"], tenant_id, PROVEDOR, chave)

    # Migração preguiçosa (item 1.7): linha antiga em texto puro é regravada
    # cifrada na primeira leitura, quando TOKENS_CRYPTO_KEY está configurada.
    if chave and not (esta_cifrado(data["access_token"]) and esta_cifrado(data["refresh_token"])):
        (
            vertical.table("integracoes_oauth")
            .update(
                {
                    "access_token": cifrar(access_token, tenant_id, PROVEDOR, chave),
                    "refresh_token": cifrar(refresh_token, tenant_id, PROVEDOR, chave),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("tenant_id", tenant_id)
            .eq("provedor", PROVEDOR)
            .execute()
        )

    expira = datetime.fromisoformat(data["token_expira_em"])
    # Se o token expira em menos de 5 minutos, renovar proativamente
    agora = datetime.now(timezone.utc) + timedelta(minutes=5)

    if expira > agora:
        return access_token, data["calendar_id"] or "primary"

    # Token expirado ou prestes a expirar — refresh
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(
            GOOGLE_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
        tokens = resp.json()

    if "access_token" not in tokens:
        # Só o código de erro do Google: a resposta pode trazer dados da conta.
        log.error(
            "Falha ao renovar token Google | tenant=%s | erro=%s", tenant_id, tokens.get("error")
        )
        return None

    nova_expiracao = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))

    (
        vertical.table("integracoes_oauth")
        .update(
            {
                "access_token": cifrar(tokens["access_token"], tenant_id, PROVEDOR, chave),
                "token_expira_em": nova_expiracao.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("tenant_id", tenant_id)
        .eq("provedor", "google_calendar")
        .execute()
    )

    return tokens["access_token"], data["calendar_id"] or "primary"


async def criar_evento(
    tenant_id: str,
    titulo: str,
    inicio_utc: str,
    fim_utc: str,
    descricao: str | None = None,
    local: str | None = None,
) -> str | None:
    """Cria evento no Google Calendar do tenant.

    Retorna google_event_id ou None se falhar / integração não conectada.
    """
    tokens = await _get_valid_access_token(tenant_id)
    if not tokens:
        log.info("Google Calendar não conectado | tenant=%s", tenant_id)
        return None

    access_token, calendar_id = tokens

    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.post(
            f"{GOOGLE_CALENDAR_URL}/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "summary": titulo,
                "description": descricao,
                "location": local,
                "start": {
                    "dateTime": inicio_utc,
                    "timeZone": "America/Campo_Grande",
                },
                "end": {
                    "dateTime": fim_utc,
                    "timeZone": "America/Campo_Grande",
                },
            },
        )
        evento = resp.json()

    event_id = evento.get("id")
    if event_id:
        log.info(
            "Evento criado no Google Calendar | tenant=%s | event_id=%s",
            tenant_id,
            event_id,
        )
    else:
        log.error("Falha ao criar evento | tenant=%s | resp=%s", tenant_id, evento)

    return event_id


async def deletar_evento(tenant_id: str, event_id: str) -> None:
    """Remove evento do Google Calendar. Silencioso se falhar."""
    try:
        tokens = await _get_valid_access_token(tenant_id)
        if not tokens:
            return
        access_token, calendar_id = tokens
        async with httpx.AsyncClient(timeout=10) as http:
            await http.delete(
                f"{GOOGLE_CALENDAR_URL}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        log.info("Evento removido do Google Calendar | event_id=%s", event_id)
    except Exception as e:
        log.warning("Falha ao deletar evento Google | event_id=%s: %s", event_id, e)
