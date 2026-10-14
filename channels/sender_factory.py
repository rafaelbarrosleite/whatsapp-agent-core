"""channels/sender_factory.py — cria o sender a partir do provider e das
credenciais do canal da clínica (channels/credenciais.py, sem fallback global).

Canal único: API oficial da Meta (cloud_api). Qualquer outro provider é
recusado — a Evolution API foi removida por decisão do fundador (26/09/2026).
"""

from channels.cloud_api_sender import CloudApiSender
from channels.credenciais import resolver_credenciais_meta


def criar_sender(
    provider: str,
    credenciais_ref: str | None,
    *,
    tenant_id: str | None = None,
    phone_number_id: str | None = None,
    token_cifrado: str | None = None,
) -> CloudApiSender:
    """Retorna CloudApiSender com as credenciais do canal.

    Levanta ValueError para provider que não seja cloud_api (dado corrompido em
    canais_whatsapp) e CredencialCanalAusenteError se o canal não tem credencial.
    """
    if provider == "cloud_api":
        numero_id, token = resolver_credenciais_meta(
            credenciais_ref,
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            token_cifrado=token_cifrado,
        )
        return CloudApiSender(phone_number_id=numero_id, token=token)

    raise ValueError(f"Provider não suportado: {provider!r} (somente cloud_api)")
