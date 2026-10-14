"""Testes unitários para security/hmac_meta.py.

Exercitam só a função pura validar_assinatura_meta — sem FastAPI, sem Supabase.
"""

import hashlib
import hmac

from security.hmac_meta import validar_assinatura_meta

_SECRET = "app-secret-de-teste"
_BODY = b'{"object":"whatsapp_business_account"}'


def _assinar(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ─── Casos válidos ────────────────────────────────────────────────────────────


def test_assinatura_valida_retorna_true() -> None:
    sig = _assinar(_BODY, _SECRET)
    assert validar_assinatura_meta(_BODY, sig, _SECRET) is True


def test_assinatura_valida_body_diferente() -> None:
    outro_body = b'{"object":"page","entry":[]}'
    sig = _assinar(outro_body, _SECRET)
    assert validar_assinatura_meta(outro_body, sig, _SECRET) is True


# ─── Header ausente / malformado ─────────────────────────────────────────────


def test_header_ausente_retorna_false() -> None:
    assert validar_assinatura_meta(_BODY, "", _SECRET) is False


def test_header_sem_prefixo_sha256_retorna_false() -> None:
    digest = hmac.new(_SECRET.encode(), _BODY, hashlib.sha256).hexdigest()
    assert validar_assinatura_meta(_BODY, digest, _SECRET) is False  # sem "sha256="


def test_header_com_prefixo_errado_retorna_false() -> None:
    digest = hmac.new(_SECRET.encode(), _BODY, hashlib.sha256).hexdigest()
    assert validar_assinatura_meta(_BODY, f"md5={digest}", _SECRET) is False


# ─── Assinatura inválida ──────────────────────────────────────────────────────


def test_secret_errado_retorna_false() -> None:
    sig = _assinar(_BODY, "outro-secret")
    assert validar_assinatura_meta(_BODY, sig, _SECRET) is False


def test_body_adulterado_retorna_false() -> None:
    sig = _assinar(_BODY, _SECRET)
    body_adulterado = _BODY + b" "
    assert validar_assinatura_meta(body_adulterado, sig, _SECRET) is False


def test_hash_truncado_retorna_false() -> None:
    sig = _assinar(_BODY, _SECRET)
    sig_truncada = sig[:20]
    assert validar_assinatura_meta(_BODY, sig_truncada, _SECRET) is False


def test_hash_em_maiusculas_retorna_false() -> None:
    # A Meta envia em hexadecimal minúsculo; maiúsculas não devem passar.
    sig = _assinar(_BODY, _SECRET).upper()
    assert validar_assinatura_meta(_BODY, sig, _SECRET) is False
