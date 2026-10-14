"""gateway/historico_coexistencia.py — coexistência: histórico e agenda do app
WhatsApp Business, enviados pela Meta quando a clínica conecta o número.

O webhook só registra o evento (idempotência) e agenda esta tarefa. As regras
de gravação estão em db/historico.py. A sincronização precisa ser PEDIDA à Meta
(POST /admin/coexistencia/sincronizar) nas primeiras 24 h após conectar o número.
"""

import asyncio
import logging

from channels.base import ContatoAgenda, ConversaHistorico
from core.observability import bind_tenant
from db.client import get_supabase
from db.historico import aplicar_contato_agenda, importar_conversa_historico
from db.idempotency import finish_event
from gateway.tenant_resolver import TenantInfo

logger = logging.getLogger(__name__)

PROVIDER_HISTORICO = "cloud_api_historico"

# Um import por clínica de cada vez: dois pedaços do histórico do mesmo
# paciente chegando juntos não criam duas conversas de histórico.
_travas: dict[str, asyncio.Lock] = {}


def _trava(tenant_id: str) -> asyncio.Lock:
    trava = _travas.get(tenant_id)
    if trava is None:
        trava = _travas[tenant_id] = asyncio.Lock()
    return trava


def _importar(
    tenant: TenantInfo, conversas: list[ConversaHistorico], contatos: list[ContatoAgenda]
) -> tuple[int, int, int]:
    client = get_supabase()
    mensagens = 0
    falhas = 0
    for conversa in conversas:
        try:
            mensagens += importar_conversa_historico(
                client, tenant.tenant_id, tenant.canal_id, conversa
            )
        except Exception:
            falhas += 1
            logger.exception(
                "Falha ao importar histórico de um paciente | tenant=%s", tenant.tenant_id
            )
    nomes = 0
    for contato in contatos:
        try:
            nomes += int(aplicar_contato_agenda(client, tenant.tenant_id, contato))
        except Exception:
            falhas += 1
            logger.exception("Falha ao aplicar contato da agenda | tenant=%s", tenant.tenant_id)
    return mensagens, nomes, falhas


async def processar_sincronizacao_app(
    *,
    tenant: TenantInfo,
    conversas: list[ConversaHistorico],
    contatos: list[ContatoAgenda],
    event_id: str,
) -> None:
    bind_tenant(tenant.tenant_id)
    async with _trava(tenant.tenant_id):
        # Banco síncrono numa thread: um histórico grande não trava o webhook.
        mensagens, nomes, falhas = await asyncio.to_thread(_importar, tenant, conversas, contatos)
    sucesso = falhas == 0
    try:
        finish_event(
            get_supabase(),
            provider=PROVIDER_HISTORICO,
            event_id=event_id,
            success=sucesso,
            error=None if sucesso else f"{falhas} item(ns) com falha",
        )
    except Exception:
        logger.exception("Falha ao finalizar evento de histórico | tenant=%s", tenant.tenant_id)
    # Só contagens: o conteúdo é dado de saúde (LGPD).
    logger.info(
        "Histórico do app importado | tenant=%s | pacientes=%d | mensagens=%d | "
        "nomes_da_agenda=%d | falhas=%d",
        tenant.tenant_id,
        len(conversas),
        mensagens,
        nomes,
        falhas,
    )
