"""Testes para channels/dispatcher.py.

Item 1.8: cada clínica envia pelo próprio canal — o da conversa, senão o
principal, senão o ativo mais antigo. Sem canal ou sem credencial, nada é
enviado (sem fallback para o número global)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.cloud_api_sender import CloudApiSender
from channels.dispatcher import enviar_mensagem_tenant, enviar_midia_tenant, escolher_canal

_PATCH_CRIAR_SENDER = "channels.dispatcher.criar_sender"


def _mock_core(rows: list[dict]) -> MagicMock:
    core = MagicMock()
    core.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = rows
    return core


def _canal(id_: str, ref: str, principal: bool = False, created_at: str = "2026-01-01") -> dict:
    return {
        "id": id_,
        "provider": "cloud_api",
        "credenciais_ref": ref,
        "principal": principal,
        "created_at": created_at,
    }


# ─── escolha do canal ────────────────────────────────────────────────────────


def test_escolhe_o_canal_da_conversa_quando_informado() -> None:
    canais = [_canal("a", "A", principal=True), _canal("b", "B")]
    assert escolher_canal(canais, "b")["id"] == "b"


def test_sem_canal_da_conversa_usa_o_principal() -> None:
    canais = [
        _canal("antigo", "A", created_at="2025-01-01"),
        _canal("novo", "B", principal=True, created_at="2026-09-01"),
    ]
    assert escolher_canal(canais)["id"] == "novo"
    # canal_id que não está entre os ativos: também cai no principal
    assert escolher_canal(canais, "inativo")["id"] == "novo"


def test_sem_principal_usa_o_ativo_mais_antigo() -> None:
    canais = [
        _canal("novo", "B", created_at="2026-09-01"),
        _canal("antigo", "A", created_at="2025-01-01"),
    ]
    assert escolher_canal(canais)["id"] == "antigo"


def test_sem_canais_nao_escolhe_nenhum() -> None:
    assert escolher_canal([]) is None


# ─── envio ───────────────────────────────────────────────────────────────────


async def test_envia_pelo_canal_principal_da_clinica() -> None:
    core = _mock_core([_canal("a", "CLINICA_A"), _canal("b", "CLINICA_B", principal=True)])
    sender_mock = MagicMock()
    sender_mock.enviar_texto = AsyncMock(return_value=True)

    with patch(_PATCH_CRIAR_SENDER, return_value=sender_mock) as mock_factory:
        resultado = await enviar_mensagem_tenant("tenant-1", "+5567999990000", "oi", core)

    assert resultado is True
    assert mock_factory.call_args.args == ("cloud_api", "CLINICA_B")
    assert mock_factory.call_args.kwargs["tenant_id"] == "tenant-1"
    core.table.return_value.select.return_value.eq.assert_called_with("tenant_id", "tenant-1")


async def test_resposta_de_conversa_vai_pelo_numero_em_que_ela_chegou() -> None:
    core = _mock_core([_canal("a", "CLINICA_A", principal=True), _canal("b", "CLINICA_B")])
    sender_mock = MagicMock()
    sender_mock.enviar_texto = AsyncMock(return_value=True)

    with patch(_PATCH_CRIAR_SENDER, return_value=sender_mock) as mock_factory:
        await enviar_mensagem_tenant("tenant-1", "+5567999990000", "oi", core, canal_id="b")

    assert mock_factory.call_args.args == ("cloud_api", "CLINICA_B")


async def test_sem_canal_ativo_nao_envia_nada() -> None:
    core = _mock_core([])
    with patch(_PATCH_CRIAR_SENDER) as mock_factory:
        resultado = await enviar_mensagem_tenant("tenant-sem-canal", "+5567999990000", "oi", core)
    assert resultado is False
    mock_factory.assert_not_called()


async def test_canal_sem_credencial_nao_envia_pelo_numero_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "global")
    monkeypatch.setenv("WHATSAPP_CLOUD_TOKEN", "global")
    core = _mock_core([_canal("a", "CLINICA_SEM_ENV", principal=True)])
    with patch.object(CloudApiSender, "enviar_texto", AsyncMock(return_value=True)) as envio:
        resultado = await enviar_mensagem_tenant("tenant-1", "+5567999990000", "oi", core)
    assert resultado is False
    envio.assert_not_called()


async def test_canal_legado_evolution_nao_envia() -> None:
    """Evolution foi removida: um canal legado marcado 'evolution' não envia."""
    core = _mock_core(
        [{"id": "x", "provider": "evolution", "credenciais_ref": "X", "principal": True}]
    )
    with patch.object(CloudApiSender, "enviar_texto", AsyncMock(return_value=True)) as envio:
        resultado = await enviar_mensagem_tenant("t-legado", "+5567999990000", "oi", core)
    assert resultado is False
    envio.assert_not_called()


async def test_integracao_cria_cloud_api_sender_com_a_credencial_do_canal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLINICA_A_PHONE_NUMBER_ID", "111")
    monkeypatch.setenv("CLINICA_A_TOKEN", "tok-a")
    core = _mock_core([_canal("a", "CLINICA_A", principal=True)])
    captured: dict = {}

    async def _capturar(self, numero, texto):
        captured["sender"] = self
        return True

    with patch.object(CloudApiSender, "enviar_texto", _capturar):
        assert await enviar_mensagem_tenant("t", "+5567999990000", "oi", core) is True
    assert captured["sender"].token == "tok-a"


async def test_midia_sem_canal_nao_envia() -> None:
    core = _mock_core([])
    assert await enviar_midia_tenant("t", "+5567999990000", "https://x", "image", core) is False
