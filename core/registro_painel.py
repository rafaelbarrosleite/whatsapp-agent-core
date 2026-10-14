"""core/registro_painel.py — grava consumo de IA e erros por clínica para o
painel da área administrativa do CRM (Fase C: indicadores e saúde).

- `registrar_uso_ia`: uma linha em `uso_ia` por chamada ao Claude, com os
  tokens separados (entrada sem cache, saída, leitura e escrita de cache).
  O custo em dólar é calculado no CRM, que tem a tabela de preços.
- `registrar_erro`: uma linha em `erros_motor` (envio que falhou, canal sem
  credencial, exceção no agente). O detalhe é um texto FIXO escrito no código
  ou o nome da exceção — nunca conteúdo de mensagem, telefone ou nome (LGPD).

Tudo é best-effort e fora do caminho da resposta: a gravação roda numa thread
à parte e qualquer falha só vira log. O painel pode perder uma linha; o
paciente nunca espera por ele.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

logger = logging.getLogger(__name__)

UsoIA = Literal["atendimento", "interno", "briefing", "analise"]
TipoErro = Literal["envio", "canal", "agente"]

_DETALHE_MAX = 300
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="registro-painel")


def tokens_da_resposta(resposta: Any) -> dict[str, int] | None:
    """Tokens de uma resposta do LangChain, separados como a Anthropic cobra.

    `input_tokens` do LangChain é o TOTAL de entrada; cache_read e
    cache_creation são parte dele. Por isso a entrada "cheia" é o total menos
    as duas parcelas de cache. Devolve None se a resposta não tem uso.
    """
    uso = getattr(resposta, "usage_metadata", None) or {}
    if not uso:
        return None
    detalhes = uso.get("input_token_details") or {}
    leitura = int(detalhes.get("cache_read") or 0)
    escrita = int(detalhes.get("cache_creation") or 0)
    total = int(uso.get("input_tokens") or 0)
    return {
        "tokens_entrada": max(total - leitura - escrita, 0),
        "tokens_saida": int(uso.get("output_tokens") or 0),
        "cache_leitura": leitura,
        "cache_escrita": escrita,
    }


def _inserir(tabela: str, linha: dict) -> None:
    try:
        from db.vertical_client import get_vertical_supabase

        get_vertical_supabase().table(tabela).insert(linha).execute()
    except Exception:  # o painel nunca derruba o atendimento
        logger.warning("Falha ao gravar %s para o painel", tabela, exc_info=True)


def _agendar(tabela: str, linha: dict) -> None:
    try:
        _executor.submit(_inserir, tabela, linha)
    except Exception:
        logger.warning("Falha ao agendar gravação de %s", tabela, exc_info=True)


def registrar_uso_ia(tenant_id: str | None, uso: UsoIA, modelo: str, resposta: Any) -> None:
    """Registra os tokens de uma chamada ao Claude para a clínica."""
    if not tenant_id:
        return
    tokens = tokens_da_resposta(resposta)
    if tokens is None:
        return
    _agendar("uso_ia", {"tenant_id": tenant_id, "uso": uso, "modelo": modelo, **tokens})


def registrar_erro(tenant_id: str | None, tipo: TipoErro, detalhe: str) -> None:
    """Registra um erro da clínica. `detalhe` deve ser texto fixo, sem dados pessoais."""
    if not tenant_id:
        return
    _agendar(
        "erros_motor",
        {"tenant_id": tenant_id, "tipo": tipo, "detalhe": detalhe[:_DETALHE_MAX]},
    )
