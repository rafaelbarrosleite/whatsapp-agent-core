"""Fase A do onboarding: clínica nova não herda textos de outra clínica nem dispara
mensagens sem ter configurado."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from core.pipeline import _apresentacao_padrao, _mensagem_apresentacao_inicial
from jobs.reengajamento import _dentro_da_janela, _mensagem, _reengajamento_ativo


def _core_tenant(row: dict) -> MagicMock:
    core = MagicMock()
    core.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        row
    ]
    return core


def test_saudacao_padrao_usa_nome_da_clinica_e_do_agente() -> None:
    texto = _mensagem_apresentacao_inicial(
        _core_tenant({"nome": "Clínica Sorriso", "config": {"nome_agente": "LIA"}}), "t-1"
    )
    assert "*LIA*" in texto
    assert "Clínica Sorriso" in texto
    for herdado in ("Dr. Fulano", "ortopedista", "Sao Paulo"):
        assert herdado not in texto


def test_saudacao_configurada_tem_prioridade() -> None:
    config = {"atendimento": {"mensagem_apresentacao_inicial": "Oi da clínica!"}}
    assert _mensagem_apresentacao_inicial(_core_tenant({"nome": "X", "config": config}), "t-1") == (
        "Oi da clínica!"
    )


def test_saudacao_sem_nome_da_clinica() -> None:
    assert "assistente virtual 😊" in _apresentacao_padrao("", "agente")


def test_reengajamento_desligado_por_padrao() -> None:
    assert _reengajamento_ativo({}) is False
    assert _reengajamento_ativo({"notificacoes": {}}) is False
    assert _reengajamento_ativo({"notificacoes": {"reengajamento": True}}) is True


def test_mensagem_de_reengajamento_sem_nome_de_medico() -> None:
    assert "Dr. Fulano" not in _mensagem({})
    assert _mensagem({"reengajamento": {"mensagem": " Texto da clínica "}}) == "Texto da clínica"


def test_janela_de_reengajamento_no_fuso_da_clinica() -> None:
    # 23:30 UTC = 19:30 em UTC-4 (dentro) e 20:30 em UTC-3 (fora).
    agora = datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)
    assert _dentro_da_janela(agora, {"lembrete": {"utc_offset_horas": -4}}) is True
    assert _dentro_da_janela(agora, {}) is False
