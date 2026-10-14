"""gateway/rajada.py — junta a rajada de mensagens do paciente antes de responder.

Problema: o paciente manda "oi", "quero marcar" e "pode ser amanhã?" em 3
segundos. Antes, cada mensagem virava uma tarefa independente e o agente rodava
três vezes EM PARALELO na mesma conversa: três respostas, histórico embaralhado.

Solução (por conversa = tenant + telefone do paciente):
1. A mensagem entra numa fila em memória da conversa.
2. Um único "dono" da fila espera o paciente parar de digitar (janela de
   silêncio, `debounce_mensagens_segundos`), com teto (`debounce_maximo_segundos`)
   para nunca segurar a resposta indefinidamente.
3. Processa o lote de uma vez. Mensagem que chega enquanto o agente responde
   espera na fila e vira o próximo lote: nunca há duas execuções simultâneas
   na mesma conversa.

A fila vive no processo (o motor roda com 1 réplica — ver docs). O webhook já
respondeu 200 à Meta antes da espera. Num deploy, o uvicorn espera as tarefas em
andamento terminarem; se o container for derrubado antes disso (o Docker mata
após ~10 s), a mensagem que estava na fila não é respondida. É o mesmo risco que
já existia para a mensagem em processamento, agora com até 3 s a mais.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from channels.base import MensagemRecebida
from gateway.tenant_resolver import TenantInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemRajada:
    """Uma mensagem já registrada na idempotência, aguardando processamento."""

    event_id: str
    mensagem: MensagemRecebida
    tenant: TenantInfo
    sender: Any
    provider: str


@dataclass
class _Fila:
    criada_em: float
    itens: list[ItemRajada] = field(default_factory=list)
    primeira_em: float = 0.0
    ultima_em: float = 0.0
    dono_ativo: bool = False


# Fila criada há mais que isto e cujo dono nunca começou é órfã: a tarefa de
# fundo do request que a criou não rodou (ex.: a conexão caiu antes da
# resposta). A próxima mensagem agenda um novo dono, sem perder o que está nela.
_ORFA_APOS_SEGUNDOS = 30.0


ProcessarLote = Callable[[list[ItemRajada]], Awaitable[None]]

# chave da conversa → fila. Só existe enquanto há um dono drenando a fila.
_filas: dict[str, _Fila] = {}


def chave_conversa(tenant_id: str, telefone: str) -> str:
    """Mesma chave do thread_id do LangGraph (core/pipeline)."""
    return f"{tenant_id}:{telefone}"


def _agora() -> float:
    return asyncio.get_running_loop().time()


def adicionar(item: ItemRajada) -> str | None:
    """Põe a mensagem na fila da conversa.

    Retorna a chave quando a fila é NOVA — quem chamou deve agendar
    `drenar(chave, ...)`. Retorna None quando já existe um dono drenando essa
    conversa (a mensagem entra no lote dele).

    Síncrono de propósito: roda dentro do request do webhook, antes de qualquer
    `await`, então não há corrida entre "fila existe?" e "criar fila".
    """
    chave = chave_conversa(item.tenant.tenant_id, item.mensagem.numero_remetente)
    agora = _agora()
    fila = _filas.get(chave)
    precisa_dono = fila is None
    if fila is None:
        fila = _filas[chave] = _Fila(criada_em=agora)
    elif not fila.dono_ativo and agora - fila.criada_em > _ORFA_APOS_SEGUNDOS:
        logger.warning("Fila de conversa sem dono — agendando novo | itens=%d", len(fila.itens))
        fila.criada_em = agora
        precisa_dono = True
    if not fila.itens:
        fila.primeira_em = agora
    fila.itens.append(item)
    fila.ultima_em = agora
    return chave if precisa_dono else None


async def _aguardar_silencio(fila: _Fila) -> None:
    """Espera até o paciente ficar `debounce` segundos sem mandar nada, ou até
    o teto desde a primeira mensagem do lote."""
    janela = max(0.0, float(settings.debounce_mensagens_segundos))
    teto = max(janela, float(settings.debounce_maximo_segundos))
    while True:
        agora = _agora()
        prazo = min(fila.ultima_em + janela, fila.primeira_em + teto)
        if agora >= prazo:
            return
        await asyncio.sleep(prazo - agora)


async def drenar(chave: str, processar_lote: ProcessarLote) -> None:
    """Dono da fila: espera a rajada terminar, processa, repete até esvaziar."""
    fila = _filas.get(chave)
    if fila is None or fila.dono_ativo:
        return
    fila.dono_ativo = True
    try:
        while fila.itens:
            await _aguardar_silencio(fila)
            lote, fila.itens = fila.itens, []
            try:
                await processar_lote(lote)
            except Exception:
                # processar_lote já marca cada evento; isto é só rede de segurança
                # para a fila nunca ficar órfã (sem dono) por causa de um erro.
                logger.exception("Falha ao processar lote da conversa | itens=%d", len(lote))
    finally:
        # Sem `await` entre o último `while fila.itens` e a remoção: nenhuma
        # mensagem nova consegue entrar numa fila sem dono.
        if _filas.get(chave) is fila:
            if fila.itens:
                # Só acontece se o loop foi cancelado com itens pendentes.
                logger.error(
                    "Fila da conversa encerrada com mensagens pendentes | itens=%d",
                    len(fila.itens),
                )
            del _filas[chave]


async def drenar_varias(chaves: list[str], processar_lote: ProcessarLote) -> None:
    """Drena várias conversas em paralelo (um payload pode ter pacientes
    diferentes; um não espera o outro)."""
    await asyncio.gather(*(drenar(chave, processar_lote) for chave in chaves))
