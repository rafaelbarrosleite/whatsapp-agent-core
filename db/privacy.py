import logging
from typing import Literal

from supabase import Client

log = logging.getLogger(__name__)

Channel = Literal["whatsapp", "email", "sms"]
Purpose = Literal[
    "atendimento_transacional",
    "lembretes",
    "followup_comercial",
    "nps",
    "campanhas",
]


def get_active_term(vertical: Client, tenant_id: str) -> dict | None:
    result = (
        vertical.table("lgpd_termos")
        .select("id, titulo, versao, conteudo_hash")
        .eq("tenant_id", tenant_id)
        .eq("tipo", "consentimento_cliente")
        .eq("ativo", True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def record_preference(
    vertical: Client,
    *,
    tenant_id: str,
    contato_id: str,
    channel: Channel,
    purpose: Purpose,
    decision: Literal["aceito", "recusado", "revogado"],
    origin: str,
    term: dict | None,
) -> None:
    vertical.rpc(
        "registrar_preferencia_lgpd",
        {
            "p_tenant_id": tenant_id,
            "p_contato_id": contato_id,
            "p_canal": channel,
            "p_finalidade": purpose,
            "p_decisao": decision,
            "p_termo_id": term.get("id") if term else None,
            "p_termo_versao": term.get("versao") if term else None,
            "p_termo_hash": term.get("conteudo_hash") if term else None,
            "p_origem": origin,
        },
    ).execute()


def communication_allowed(
    vertical: Client,
    *,
    tenant_id: str,
    contato_id: str,
    channel: Channel,
    purpose: Purpose,
    current_term: dict | None = None,
    transactional_fallback: bool = False,
) -> bool:
    """Fail closed unless an applicable, current acceptance exists.

    Appointment reminders may explicitly use the transactional acceptance as a
    fallback. A purpose-specific refusal/revocation always overrides fallback.
    """
    result = (
        vertical.table("lgpd_preferencias")
        .select("decisao, termo_versao, termo_hash, finalidade")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .eq("canal", channel)
        .eq("finalidade", purpose)
        .limit(1)
        .execute()
    )
    preference = result.data[0] if result.data else None
    if preference:
        if preference.get("decisao") != "aceito":
            return False
        return _matches_current_term(preference, current_term)

    if not transactional_fallback or purpose == "atendimento_transacional":
        return False

    fallback = (
        vertical.table("lgpd_preferencias")
        .select("decisao, termo_versao, termo_hash, finalidade")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .eq("canal", channel)
        .eq("finalidade", "atendimento_transacional")
        .limit(1)
        .execute()
    )
    preference = fallback.data[0] if fallback.data else None
    return bool(
        preference
        and preference.get("decisao") == "aceito"
        and _matches_current_term(preference, current_term)
    )


def _matches_current_term(preference: dict, current_term: dict | None) -> bool:
    if current_term is None:
        return False
    return preference.get("termo_versao") == current_term.get("versao") and preference.get(
        "termo_hash"
    ) == current_term.get("conteudo_hash")
