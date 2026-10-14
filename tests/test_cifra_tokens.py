"""Cifra dos tokens OAuth (item 1.7) — mesmo formato do CRM (lib/cifra-tokens.ts)."""

import base64

import pytest

from security.cifra_tokens import ChaveTokensAusenteError, cifrar, decifrar, esta_cifrado

CHAVE = base64.b64encode(bytes(range(32))).decode()
TENANT = "11111111-1111-4111-8111-111111111111"
PROVEDOR = "google_calendar"


def test_ida_e_volta_sem_token_em_claro():
    cifrado = cifrar("ya29.segredo", TENANT, PROVEDOR, CHAVE)
    assert esta_cifrado(cifrado)
    assert "ya29" not in cifrado
    assert decifrar(cifrado, TENANT, PROVEDOR, CHAVE) == "ya29.segredo"


def test_decifra_vetor_gerado_pelo_crm_em_typescript():
    do_crm = "v1:7B1wvRfNYdBHc-rhgjuUDzY--qZ5fFyyIWUynyJFptKGVooarrI6L-wAAIA7"
    assert decifrar(do_crm, TENANT, PROVEDOR, CHAVE) == "ya29.token-do-crm"


def test_token_copiado_para_outra_clinica_nao_decifra():
    cifrado = cifrar("ya29.segredo", TENANT, PROVEDOR, CHAVE)
    with pytest.raises(Exception):
        decifrar(cifrado, "22222222-2222-4222-8222-222222222222", PROVEDOR, CHAVE)


def test_texto_puro_antigo_e_sem_chave():
    assert decifrar("ya29.antigo", TENANT, PROVEDOR, CHAVE) == "ya29.antigo"
    assert cifrar("ya29.x", TENANT, PROVEDOR, None) == "ya29.x"
    cifrado = cifrar("ya29.segredo", TENANT, PROVEDOR, CHAVE)
    with pytest.raises(ChaveTokensAusenteError):
        decifrar(cifrado, TENANT, PROVEDOR, None)


def test_nao_cifra_duas_vezes():
    cifrado = cifrar("ya29.segredo", TENANT, PROVEDOR, CHAVE)
    assert cifrar(cifrado, TENANT, PROVEDOR, CHAVE) == cifrado
