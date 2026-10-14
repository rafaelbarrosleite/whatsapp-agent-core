"""LGPD: a SAÍDA de log (formatter JSON de produção e alerta de WhatsApp) nunca
leva telefone de paciente nem a mensagem de exceções.

Complementa tests/test_logs_sem_conteudo.py (que garante que as chamadas de log
não passam conteúdo): aqui a garantia vale mesmo para uma chamada mal escrita.
"""

import json
import logging
import sys
from unittest.mock import MagicMock, patch

from pydantic import BaseModel, ValidationError

from core.error_notifier import WhatsAppErrorHandler
from core.observability import (
    JsonFormatter,
    descrever_excecao,
    formatar_excecao_sem_conteudo,
    mascarar_telefones,
)

TELEFONE = "5567988887777"
TEXTO_PACIENTE = "DOR-NO-PEITO-MARCADOR-c41d"
TENANT = "0b5f3c2e-1a2b-4c3d-9e8f-001122334455"


def _record(msg: str, args=(), exc_info=None, level=logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="core.pipeline",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def _exc_info_com_conteudo():
    try:
        raise ValueError(f"paciente {TELEFONE} disse {TEXTO_PACIENTE}")
    except ValueError:
        return sys.exc_info()


# ── mascaramento ────────────────────────────────────────────────────────────


def test_mascarar_telefones_formatos_comuns():
    for bruto in (TELEFONE, "+" + TELEFONE, "67988887777", "6798888777"):
        saida = mascarar_telefones(f"de={bruto} ok")
        assert bruto not in saida
        assert saida == f"de=***{bruto[-4:]} ok"


def test_mascarar_telefones_no_thread_id():
    saida = mascarar_telefones(f"thread={TENANT}:{TELEFONE}")
    assert TELEFONE not in saida
    assert TENANT in saida  # o tenant continua rastreável


def test_mascarar_telefones_preserva_numeros_curtos_e_ids():
    texto = "chars=42 | status=503 | message_id=wamid.HBgNNTU2Nzk4ODg4Nzc3NxUCABIYFjNFQjA"
    assert mascarar_telefones(texto) == texto


# ── exceções ───────────────────────────────────────────────────────────────


def test_descrever_excecao_sem_mensagem_com_code():
    exc = RuntimeError(f"falhou para {TEXTO_PACIENTE}")
    exc.code = "42P01"
    assert descrever_excecao(exc) == "RuntimeError[code=42P01]"


def test_traceback_sem_mensagem_mas_com_pilha():
    saida = formatar_excecao_sem_conteudo(_exc_info_com_conteudo())
    assert TEXTO_PACIENTE not in saida
    assert TELEFONE not in saida
    assert "ValueError" in saida
    assert "test_logs_sem_pii_saida.py" in saida


def test_traceback_de_validation_error_nao_vaza_input():
    class Msg(BaseModel):
        idade: int

    try:
        Msg(idade=TEXTO_PACIENTE)
    except ValidationError:
        saida = formatar_excecao_sem_conteudo(sys.exc_info())
    assert TEXTO_PACIENTE not in saida
    assert "ValidationError" in saida


def test_cadeia_de_excecoes_so_tipos():
    try:
        try:
            raise KeyError(TEXTO_PACIENTE)
        except KeyError as e:
            raise RuntimeError(TELEFONE) from e
    except RuntimeError:
        saida = formatar_excecao_sem_conteudo(sys.exc_info())
    assert "RuntimeError <- KeyError" in saida
    assert TEXTO_PACIENTE not in saida and TELEFONE not in saida


# ── formatter JSON (produção) ──────────────────────────────────────────────


def test_json_formatter_mascara_args_e_excecao():
    exc_info = _exc_info_com_conteudo()
    record = _record(
        "Erro em criar_agendamento | thread=%s | %s: %s",
        (f"{TENANT}:{TELEFONE}", "ValueError", exc_info[1]),
        exc_info=exc_info,
    )
    saida = JsonFormatter().format(record)
    payload = json.loads(saida)
    assert TELEFONE not in saida
    assert TEXTO_PACIENTE not in saida
    assert "***7777" in payload["message"]
    assert "ValueError" in payload["exception"]


def test_json_formatter_mensagem_pre_formatada_ainda_mascara_telefone():
    record = _record(f"Falha ao enviar para {TELEFONE}", level=logging.WARNING)
    assert TELEFONE not in JsonFormatter().format(record)


# ── alerta de WhatsApp ─────────────────────────────────────────────────────


def test_alerta_whatsapp_leva_so_modelo_e_pilha_sem_conteudo():
    handler = WhatsAppErrorHandler()
    loop = MagicMock()
    capturado: dict = {}

    def _captura(coro):
        frame = coro.cr_frame
        capturado.update(frame.f_locals)
        coro.close()

    loop.create_task.side_effect = _captura
    exc_info = _exc_info_com_conteudo()
    record = _record(
        "Erro no pipeline | thread=%s | %s",
        (f"{TENANT}:{TELEFONE}", exc_info[1]),
        exc_info=exc_info,
    )
    with patch("core.error_notifier.asyncio.get_running_loop", return_value=loop):
        handler.emit(record)

    enviado = " ".join(str(v) for v in capturado.values())
    assert "Erro no pipeline | thread=%s | %s" in enviado
    assert TELEFONE not in enviado
    assert TEXTO_PACIENTE not in enviado
    assert "ValueError" in enviado


def test_telefone_dentro_de_url_do_postgrest_e_mascarado() -> None:
    """O PostgREST recebe o filtro na URL (telefone=eq.%2B55...): o JSON de saída
    não pode levar o número inteiro, nem com o '+' codificado."""
    url = (
        "HTTP Request: GET https://x.supabase.co/rest/v1/contatos?select=id"
        f'&telefone=eq.%2B{TELEFONE}&limit=1 "HTTP/2 200 OK"'
    )
    saida = JsonFormatter().format(_record(url, level=logging.INFO))
    assert TELEFONE not in saida
    assert "***7777" in saida
    minuscula = JsonFormatter().format(_record(f"numero=eq.%2b{TELEFONE}", level=logging.INFO))
    assert TELEFONE not in minuscula


def test_uuid_e_horario_nao_sao_confundidos_com_telefone() -> None:
    texto = "id=eq.36cbde40-fb74-42c2-8309-45cea6d201c5 em 2026-10-04T22:00:55.738797"
    assert mascarar_telefones(texto) == texto


def test_bibliotecas_http_so_registram_aviso_e_erro() -> None:
    """httpx registra cada requisição em INFO com a URL inteira (com o filtro do
    PostgREST): depois de configurar o log, só WARNING para cima."""
    from core.observability import configure_structured_logging

    root = logging.getLogger()
    estado = (root.level, [(h, h.formatter) for h in root.handlers])
    niveis = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        configure_structured_logging("INFO")
        for nome in ("httpx", "httpcore"):
            logger = logging.getLogger(nome)
            assert not logger.isEnabledFor(logging.INFO)
            assert logger.isEnabledFor(logging.WARNING)
        assert logging.getLogger("core.pipeline").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(estado[0])
        for handler, formatter in estado[1]:
            handler.setFormatter(formatter)
        for nome, nivel in niveis.items():
            logging.getLogger(nome).setLevel(nivel)
