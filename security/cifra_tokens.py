"""Cifra dos tokens OAuth guardados em integracoes_oauth (item 1.7).

Formato compartilhado com o CRM (lib/cifra-tokens.ts do painel CRM):

    "v1:" + base64url(nonce[12] || texto_cifrado || tag[16])

AES-256-GCM com a chave TOKENS_CRYPTO_KEY (32 bytes em base64). O dado
associado (AAD) é "<tenant_id>:<provedor>": um token copiado para a linha de
outra clínica não decifra.

Compatibilidade: valores sem o prefixo "v1:" são tokens antigos em texto puro
e voltam como estão; quem lê regrava cifrado (migração preguiçosa).
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIXO = "v1:"


class ChaveTokensAusenteError(RuntimeError):
    """TOKENS_CRYPTO_KEY não configurada, mas há token cifrado para ler."""


def _chave(chave_b64: str | None) -> bytes | None:
    if not chave_b64:
        return None
    chave = base64.b64decode(chave_b64)
    if len(chave) != 32:
        raise ValueError("TOKENS_CRYPTO_KEY precisa ter 32 bytes (base64)")
    return chave


def _aad(tenant_id: str, provedor: str) -> bytes:
    return f"{tenant_id}:{provedor}".encode()


def esta_cifrado(valor: str | None) -> bool:
    return bool(valor) and valor.startswith(PREFIXO)


def cifrar(valor: str, tenant_id: str, provedor: str, chave_b64: str | None) -> str:
    """Cifra o token. Sem chave configurada, devolve o valor como está."""
    chave = _chave(chave_b64)
    if chave is None or esta_cifrado(valor):
        return valor
    nonce = os.urandom(12)
    cifrado = AESGCM(chave).encrypt(nonce, valor.encode(), _aad(tenant_id, provedor))
    return PREFIXO + base64.urlsafe_b64encode(nonce + cifrado).decode().rstrip("=")


def decifrar(valor: str, tenant_id: str, provedor: str, chave_b64: str | None) -> str:
    """Decifra o token; valor antigo em texto puro volta como está."""
    if not esta_cifrado(valor):
        return valor
    chave = _chave(chave_b64)
    if chave is None:
        raise ChaveTokensAusenteError("TOKENS_CRYPTO_KEY ausente para decifrar token OAuth")
    corpo = valor[len(PREFIXO) :]
    bruto = base64.urlsafe_b64decode(corpo + "=" * (-len(corpo) % 4))
    return AESGCM(chave).decrypt(bruto[:12], bruto[12:], _aad(tenant_id, provedor)).decode()
