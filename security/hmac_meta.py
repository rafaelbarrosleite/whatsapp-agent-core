"""security/hmac_meta.py — Validação de assinatura HMAC-SHA256 de webhooks da Meta.

A Meta calcula o header X-Hub-Signature-256 como:
    "sha256=" + HMAC-SHA256(key=App_Secret, msg=raw_body).hexdigest()

Referência: https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verification-requests
"""

import hashlib
import hmac


def validar_assinatura_meta(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verifica se o header X-Hub-Signature-256 é válido para o body recebido.

    Usa hmac.compare_digest para comparação timing-safe — não usar == direto.
    Retorna False para header ausente, malformado ou com hash inválido.
    """
    if not signature_header.startswith("sha256="):
        return False

    received_hash = signature_header[len("sha256=") :]
    expected_hash = hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(received_hash, expected_hash)


def validar_assinatura_meta_algum(
    body: bytes, signature_header: str, app_secrets: list[str]
) -> bool:
    """Valida se a assinatura bate com QUALQUER um dos App Secrets.

    Durante a migração para o app Tech Provider (portfólio
    IntelliAgents) chegam webhooks de dois apps, cada um assinado com o
    próprio secret.
    """
    return any(validar_assinatura_meta(body, signature_header, s) for s in app_secrets)
