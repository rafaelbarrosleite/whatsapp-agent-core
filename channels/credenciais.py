"""channels/credenciais.py — resolve as credenciais do canal de uma clínica.

Duas fontes, nesta ordem (CLAUDE.md §4.4, decisão de 04/10/2026):

1. Banco, cifrado: canais_whatsapp.phone_number_id + token_cifrado (AES-256-GCM
   com TOKENS_CRYPTO_KEY, formato de security/cifra_tokens.py, AAD
   "<tenant_id>:whatsapp_cloud_api"). É o que o assistente "Nova clínica" do
   CRM grava — nenhuma variável de ambiente nova por clínica.
2. Legado: canais_whatsapp.credenciais_ref é o prefixo de variáveis de
   ambiente (ex: "WHATSAPP_CLINICA_X" → WHATSAPP_CLINICA_X_PHONE_NUMBER_ID e
   WHATSAPP_CLINICA_X_TOKEN).

Canal único: API oficial da Meta.

Sem fallback global (item 1.8, 04/10/2026): antes, canal sem credencial
própria enviava pelo número global (WHATSAPP_PHONE_NUMBER_ID). Com mais de
uma clínica, isso mandaria mensagem de uma clínica pelo número de outra.
Agora a falta de credencial é erro explícito e nada é enviado.
"""

import logging
import os

from app.config import settings
from security.cifra_tokens import decifrar, esta_cifrado

logger = logging.getLogger(__name__)


class CredencialCanalAusenteError(Exception):
    """O canal não tem credencial própria configurada no ambiente."""

    def __init__(self, credenciais_ref: str | None) -> None:
        super().__init__(
            f"Canal WhatsApp sem credencial: defina {credenciais_ref or '<REF>'}_PHONE_NUMBER_ID "
            f"e {credenciais_ref or '<REF>'}_TOKEN"
        )
        self.credenciais_ref = credenciais_ref


# Provedor no dado associado da cifra: token copiado para outra clínica não decifra.
PROVEDOR_CIFRA = "whatsapp_cloud_api"


def resolver_credenciais_meta(
    credenciais_ref: str | None,
    *,
    tenant_id: str | None = None,
    phone_number_id: str | None = None,
    token_cifrado: str | None = None,
) -> tuple[str, str]:
    """Retorna (phone_number_id, token) do canal. Levanta CredencialCanalAusenteError."""
    if token_cifrado and phone_number_id and tenant_id:
        try:
            token = decifrar(token_cifrado, tenant_id, PROVEDOR_CIFRA, settings.tokens_crypto_key)
        except Exception:
            # Chave ausente/trocada ou token de outra clínica: nunca envia.
            logger.error("Token do canal não decifra | tenant=%s", tenant_id)
            raise CredencialCanalAusenteError(credenciais_ref) from None
        if token and esta_cifrado(token_cifrado):
            return phone_number_id, token

    ref = (credenciais_ref or "").strip()
    if ref:
        phone_number_id = os.environ.get(f"{ref}_PHONE_NUMBER_ID")
        token = os.environ.get(f"{ref}_TOKEN")
        if phone_number_id and token:
            return phone_number_id, token
    logger.error("Canal WhatsApp sem credencial própria | ref=%s", ref or "-")
    raise CredencialCanalAusenteError(ref or None)
