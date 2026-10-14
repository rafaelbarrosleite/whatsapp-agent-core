"""Anonimizador (Sprint 1, item 1.3 — portado do Tech Challenge FIAP Fase 3)."""

import json
import logging

from core.observability import JsonFormatter, mascarar_dados_pessoais
from security.anonimizacao import anonimizar, encontrar_dados_pessoais, mascarar_para_log

# ── anonimizar(): texto que sai para terceiro ou vira dataset ──────────────


def test_remove_identificadores_estruturados() -> None:
    texto = (
        "CPF 123.456.789-09, CNS 898 0012 3456 7890, RG 12.345.678-9, "
        "tel (67) 98765-4321, e-mail maria.souza@email.com, CEP 79002-100, "
        "nascida em 03/10/1980."
    )
    resultado = anonimizar(texto)
    for bruto in [
        "123.456.789-09",
        "898 0012",
        "12.345.678-9",
        "98765-4321",
        "maria.souza",
        "79002-100",
        "03/10/1980",
    ]:
        assert bruto not in resultado.texto
    assert {"CPF", "CNS", "RG", "TELEFONE", "EMAIL", "CEP", "DATA"} <= set(resultado.contagem)


def test_remove_telefone_e164_do_whatsapp() -> None:
    assert "999998888" not in anonimizar("contato +5567999998888").texto


def test_remove_nomes_rotulados_e_com_tratamento() -> None:
    texto = (
        "Paciente: Joana da Silva Prado\nMãe: Lucia Prado\n"
        "Atendida pela Dra. Fernanda Lima e acompanhada pela Sra. Rosa Maria.\n"
        "Endereço: Rua das Flores, 123"
    )
    resultado = anonimizar(texto)
    for nome in ["Joana", "Lucia", "Fernanda", "Rosa", "Rua das Flores"]:
        assert nome not in resultado.texto
    assert "Paciente: [NOME]" in resultado.texto
    assert "Dra. [MEDICO]" in resultado.texto


def test_preserva_mensagem_comum_de_atendimento() -> None:
    texto = "Quero marcar uma infiltração no joelho, quanto custa? Pode ser às 14h."
    resultado = anonimizar(texto)
    assert resultado.texto == texto
    assert resultado.total == 0


def test_reaplicar_e_idempotente() -> None:
    limpo = anonimizar("Paciente: Sra. Maria Souza\nEndereço: Rua A, 10\nCPF: 123.456.789-09").texto
    assert limpo == "Paciente: [NOME]\nEndereço: [ENDERECO]\nCPF: [CPF]"
    assert anonimizar(limpo).texto == limpo
    assert encontrar_dados_pessoais(limpo) == []


def test_texto_vazio() -> None:
    assert anonimizar("").texto == ""
    assert anonimizar(None).texto == ""  # type: ignore[arg-type]


# ── mascarar_para_log(): rede de proteção dos logs ─────────────────────────


def test_log_remove_email_e_cpf_formatado() -> None:
    saida = mascarar_para_log("lead maria@gmail.com cpf=123.456.789-09")
    assert "maria@gmail.com" not in saida
    assert "123.456.789-09" not in saida


def test_log_preserva_dado_operacional() -> None:
    # data de agendamento, phone_number_id da Meta (15 dígitos) e UUID
    texto = (
        "agendamento 15/10/2026 14:00 | phone_number_id=123456789012345 "
        "| tenant=0b5f3c2e-1a2b-4c3d-9e8f-001122334455"
    )
    assert mascarar_para_log(texto) == texto


def test_mascarar_dados_pessoais_junta_telefone_e_cpf() -> None:
    saida = mascarar_dados_pessoais("de=5567988887777 cpf 123.456.789-09")
    assert "988887777" not in saida
    assert "123.456.789-09" not in saida
    assert "***7777" in saida


def test_formatter_json_mascara_email_mesmo_em_chamada_mal_escrita() -> None:
    record = logging.LogRecord(
        name="core.pipeline",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="lead criado | email=%s",
        args=("paciente.teste@hotmail.com",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "paciente.teste" not in payload["message"]
    assert "[EMAIL]" in payload["message"]
