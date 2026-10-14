"""Testes para channels/credenciais.py e channels/sender_factory.py.

Item 1.8: sem fallback global. Canal sem credencial própria não envia.
"""

import pytest

from channels.cloud_api_sender import CloudApiSender
from channels.credenciais import CredencialCanalAusenteError, resolver_credenciais_meta
from channels.sender_factory import criar_sender

# ─── resolver_credenciais_meta ───────────────────────────────────────────────


def test_meta_com_ref_e_env_vars_usa_credenciais_do_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_OUTRA_CLINICA_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.setenv("META_OUTRA_CLINICA_TOKEN", "token-outra-clinica")
    assert resolver_credenciais_meta("META_OUTRA_CLINICA") == ("1234567890", "token-outra-clinica")


def test_sem_ref_nao_cai_no_numero_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "global-phone-id")
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "global-token")
    with pytest.raises(CredencialCanalAusenteError):
        resolver_credenciais_meta(None)
    with pytest.raises(CredencialCanalAusenteError):
        resolver_credenciais_meta("  ")


def test_ref_sem_phone_number_id_e_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caso visto em produção: só o _TOKEN definido."""
    monkeypatch.setenv("WHATSAPP_CLINICA_X_TOKEN", "t")
    monkeypatch.delenv("WHATSAPP_CLINICA_X_PHONE_NUMBER_ID", raising=False)
    with pytest.raises(CredencialCanalAusenteError) as erro:
        resolver_credenciais_meta("WHATSAPP_CLINICA_X")
    assert "WHATSAPP_CLINICA_X_PHONE_NUMBER_ID" in str(erro.value)


# ─── criar_sender ─────────────────────────────────────────────────────────────


def test_criar_sender_evolution_foi_removido() -> None:
    """Canal único: API oficial da Meta. 'evolution' é recusado."""
    with pytest.raises(ValueError):
        criar_sender("evolution", "X")


def test_criar_sender_cloud_api_retorna_cloud_api_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLINICA_A_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("CLINICA_A_TOKEN", "tok-a")
    sender = criar_sender("cloud_api", "CLINICA_A")
    assert isinstance(sender, CloudApiSender)
    assert sender.token == "tok-a"


def test_criar_sender_cloud_api_sem_credencial_levanta() -> None:
    with pytest.raises(CredencialCanalAusenteError):
        criar_sender("cloud_api", "CLINICA_SEM_ENV")


def test_criar_sender_provider_desconhecido_levanta_value_error() -> None:
    with pytest.raises(ValueError):
        criar_sender("whatsapp-web-generico", None)


# ─── Credencial no banco, cifrada (Fase A do onboarding) ──────────────────────

_CHAVE = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes, só para teste


def _cifrado(token: str, tenant_id: str) -> str:
    from security.cifra_tokens import cifrar

    return cifrar(token, tenant_id, "whatsapp_cloud_api", _CHAVE)


def test_credencial_do_banco_decifra_e_dispensa_variaveis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("channels.credenciais.settings.tokens_crypto_key", _CHAVE)
    token_cifrado = _cifrado("tok-meta", "tenant-a")
    assert token_cifrado.startswith("v1:")
    assert resolver_credenciais_meta(
        None, tenant_id="tenant-a", phone_number_id="999", token_cifrado=token_cifrado
    ) == ("999", "tok-meta")
    sender = criar_sender(
        "cloud_api", None, tenant_id="tenant-a", phone_number_id="999", token_cifrado=token_cifrado
    )
    assert sender.token == "tok-meta"


def test_token_de_outra_clinica_nao_decifra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("channels.credenciais.settings.tokens_crypto_key", _CHAVE)
    token_cifrado = _cifrado("tok-meta", "tenant-a")
    with pytest.raises(CredencialCanalAusenteError):
        resolver_credenciais_meta(
            None, tenant_id="tenant-b", phone_number_id="999", token_cifrado=token_cifrado
        )


def test_sem_chave_no_motor_nao_envia(monkeypatch: pytest.MonkeyPatch) -> None:
    token_cifrado = _cifrado("tok-meta", "tenant-a")
    monkeypatch.setattr("channels.credenciais.settings.tokens_crypto_key", None)
    with pytest.raises(CredencialCanalAusenteError):
        resolver_credenciais_meta(
            None, tenant_id="tenant-a", phone_number_id="999", token_cifrado=token_cifrado
        )


def test_token_em_texto_puro_no_banco_e_recusado(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constraint do banco já impede, mas o motor também não aceita."""
    monkeypatch.setattr("channels.credenciais.settings.tokens_crypto_key", _CHAVE)
    with pytest.raises(CredencialCanalAusenteError):
        resolver_credenciais_meta(
            None, tenant_id="tenant-a", phone_number_id="999", token_cifrado="EAAtextopuro"
        )


def test_banco_tem_prioridade_e_variaveis_continuam_como_legado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("channels.credenciais.settings.tokens_crypto_key", _CHAVE)
    monkeypatch.setenv("LEGADO_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("LEGADO_TOKEN", "tok-env")
    token_cifrado = _cifrado("tok-banco", "tenant-a")
    assert resolver_credenciais_meta(
        "LEGADO", tenant_id="tenant-a", phone_number_id="999", token_cifrado=token_cifrado
    ) == ("999", "tok-banco")
    assert resolver_credenciais_meta("LEGADO", tenant_id="tenant-a") == ("111", "tok-env")
