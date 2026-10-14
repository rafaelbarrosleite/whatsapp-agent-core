import contextvars
import json
import logging
import re
import threading
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from security.anonimizacao import mascarar_para_log

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)
tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="-")


# ── Logs sem dado de paciente (LGPD) ────────────────────────────────────────
# Rede de proteção central, aplicada a TODO log que sai do processo (formatter
# JSON e alerta de WhatsApp), independente de como cada chamada foi escrita:
#   - telefones (10–13 dígitos, com ou sem "+") viram "***1234";
#   - exceções nunca levam a MENSAGEM (str(e) pode conter texto do paciente,
#     ex.: ValidationError com input_value, erro de banco com valores da
#     linha); só tipo, `code` curto quando existir e a pilha arquivo:linha.
# Telefone solto ("+5567...", "5567...") ou dentro de URL ("%2B5567...", como o
# PostgREST recebe o filtro telefone=eq.+55...).
_TELEFONE_RE = re.compile(r"(?:(?<![\w.\-])\+?|%2[Bb])\d{10,13}(?!\w)")
_MAX_FRAMES = 12


def mascarar_telefones(texto: str) -> str:
    """Troca números de telefone por '***' + 4 últimos dígitos."""
    return _TELEFONE_RE.sub(lambda m: "***" + m.group(0)[-4:], texto)


def mascarar_dados_pessoais(texto: str) -> str:
    """Telefone + e-mail e CPF/CNS formatados (security/anonimizacao.py).
    Ponto único usado por todo texto que sai em log ou alerta."""
    return mascarar_para_log(mascarar_telefones(texto))


def descrever_excecao(exc: BaseException) -> str:
    """Tipo da exceção (+ `code` curto, ex.: SQLSTATE do PostgREST), sem mensagem."""
    nome = type(exc).__name__
    code = getattr(exc, "code", None)
    if isinstance(code, (str, int)) and len(str(code)) <= 16:
        return f"{nome}[code={code}]"
    return nome


def formatar_excecao_sem_conteudo(exc_info: Any) -> str:
    """Pilha (arquivo:linha em função) + tipos da cadeia de exceções, sem mensagens."""
    _tipo, exc, tb = exc_info
    if exc is None:
        return ""
    linhas = ["Traceback (mensagens omitidas por LGPD):"]
    for frame in traceback.extract_tb(tb)[-_MAX_FRAMES:]:
        linhas.append(f"  {frame.filename}:{frame.lineno} em {frame.name}")
    cadeia: list[str] = []
    atual: BaseException | None = exc
    vistos: set[int] = set()
    while atual is not None and id(atual) not in vistos:
        vistos.add(id(atual))
        cadeia.append(descrever_excecao(atual))
        atual = atual.__cause__ or atual.__context__
    linhas.append(" <- ".join(cadeia))
    return "\n".join(linhas)


def mensagem_segura(record: logging.LogRecord) -> str:
    """Mensagem do log com exceções nos args reduzidas ao tipo e telefones mascarados."""
    args = record.args
    if isinstance(args, tuple):
        args = tuple(descrever_excecao(a) if isinstance(a, BaseException) else a for a in args)
    elif isinstance(args, dict):
        args = {
            k: descrever_excecao(v) if isinstance(v, BaseException) else v for k, v in args.items()
        }
    msg = str(record.msg)
    try:
        texto = msg % args if args else msg
    except (TypeError, ValueError, KeyError):
        texto = msg
    return mascarar_dados_pessoais(texto)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mensagem_segura(record),
            "correlation_id": correlation_id_var.get(),
            "tenant_id": tenant_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = mascarar_dados_pessoais(
                formatar_excecao_sem_conteudo(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False)


class OperationalMetrics:
    def __init__(self) -> None:
        self._counter: Counter[str] = Counter()
        self._lock = threading.Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counter[name] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counter)


metrics = OperationalMetrics()


# Bibliotecas HTTP registram cada requisição em INFO com a URL inteira, e a URL
# do PostgREST carrega o filtro (telefone do paciente, ids). Só avisos e erros.
_LOGGERS_RUIDOSOS = ("httpx", "httpcore", "hpack")


def configure_structured_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for nome in _LOGGERS_RUIDOSOS:
        logging.getLogger(nome).setLevel(logging.WARNING)
    formatter = JsonFormatter()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(formatter)


async def correlation_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    correlation_token = correlation_id_var.set(correlation_id)
    tenant_token = tenant_id_var.set("-")
    metrics.increment("http_requests_received")
    try:
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        if response.status_code >= 500:
            metrics.increment("http_requests_failed")
        return response
    finally:
        correlation_id_var.reset(correlation_token)
        tenant_id_var.reset(tenant_token)


def bind_tenant(tenant_id: str) -> None:
    tenant_id_var.set(tenant_id)
