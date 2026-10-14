"""Registro de uso de IA e erros por clínica (painel da área administrativa)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import core.registro_painel as rp
from channels.dispatcher import _conferir_envio

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _resposta(entrada: int, saida: int, leitura: int = 0, escrita: int = 0):
    return SimpleNamespace(
        usage_metadata={
            "input_tokens": entrada,
            "output_tokens": saida,
            "input_token_details": {"cache_read": leitura, "cache_creation": escrita},
        }
    )


def test_tokens_separados_como_a_anthropic_cobra():
    # input_tokens do LangChain é o total; o cache é parte dele, não soma.
    tokens = rp.tokens_da_resposta(_resposta(1000, 200, leitura=700, escrita=100))
    assert tokens == {
        "tokens_entrada": 200,
        "tokens_saida": 200,
        "cache_leitura": 700,
        "cache_escrita": 100,
    }


def test_resposta_sem_uso_nao_registra():
    assert rp.tokens_da_resposta(SimpleNamespace()) is None
    with patch.object(rp, "_agendar") as agendar:
        rp.registrar_uso_ia(TENANT, "atendimento", "claude-sonnet-4-6", SimpleNamespace())
    agendar.assert_not_called()


def test_registra_uso_com_clinica_e_modelo():
    with patch.object(rp, "_agendar") as agendar:
        rp.registrar_uso_ia(TENANT, "atendimento", "claude-haiku-4-5", _resposta(50, 10))
    agendar.assert_called_once_with(
        "uso_ia",
        {
            "tenant_id": TENANT,
            "uso": "atendimento",
            "modelo": "claude-haiku-4-5",
            "tokens_entrada": 50,
            "tokens_saida": 10,
            "cache_leitura": 0,
            "cache_escrita": 0,
        },
    )


def test_sem_clinica_nao_registra():
    with patch.object(rp, "_agendar") as agendar:
        rp.registrar_uso_ia(None, "interno", "claude-sonnet-4-6", _resposta(1, 1))
        rp.registrar_erro("", "envio", "x")
    agendar.assert_not_called()


def test_erro_com_detalhe_curto():
    with patch.object(rp, "_agendar") as agendar:
        rp.registrar_erro(TENANT, "agente", "x" * 1000)
    tabela, linha = agendar.call_args.args
    assert tabela == "erros_motor"
    assert linha["tipo"] == "agente" and len(linha["detalhe"]) == 300


def test_falha_no_banco_nunca_propaga():
    cliente = MagicMock()
    cliente.table.side_effect = RuntimeError("banco fora")
    with patch("db.vertical_client.get_vertical_supabase", return_value=cliente):
        rp._inserir("uso_ia", {"tenant_id": TENANT})  # não levanta


def test_envio_recusado_vira_erro_da_clinica():
    with patch("channels.dispatcher.registrar_erro") as registrar:
        assert _conferir_envio(TENANT, True) is True
        registrar.assert_not_called()
        assert _conferir_envio(TENANT, False) is False
    registrar.assert_called_once_with(TENANT, "envio", "A Meta recusou o envio de uma mensagem")
