import logging

from channels.base import MensagemRecebida, TipoMensagem
from core.observability import bind_tenant, metrics
from core.pipeline import processar_mensagem_entrada
from db.client import get_supabase
from db.idempotency import finish_event
from gateway.rajada import ItemRajada
from gateway.tenant_resolver import TenantInfo

logger = logging.getLogger(__name__)


async def processar_evento_idempotente(
    *,
    provider: str,
    event_id: str,
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    sender,
) -> None:
    """Run the pipeline and persist the terminal state of the webhook claim."""
    await processar_lote_idempotente(
        [
            ItemRajada(
                event_id=event_id,
                mensagem=mensagem,
                tenant=tenant,
                sender=sender,
                provider=provider,
            )
        ]
    )


def _agrupavel(item: ItemRajada) -> bool:
    """Só texto digitado entra na junção. Botão (LGPD, lembrete), áudio (precisa
    transcrever) e mídia (download/prontuário) seguem um por um, na ordem."""
    m = item.mensagem
    return m.tipo == TipoMensagem.texto and bool(m.texto) and not m.metadata.get("button_id")


def agrupar_lote(itens: list[ItemRajada]) -> list[list[ItemRajada]]:
    """Divide o lote em grupos consecutivos: textos seguidos viram um grupo;
    qualquer outro tipo fica sozinho. A ordem de chegada é preservada."""
    grupos: list[list[ItemRajada]] = []
    for item in itens:
        if grupos and _agrupavel(item) and all(_agrupavel(i) for i in grupos[-1]):
            grupos[-1].append(item)
        else:
            grupos.append([item])
    return grupos


def combinar_mensagens(grupo: list[ItemRajada]) -> MensagemRecebida:
    """Um grupo de textos vira UMA mensagem para o agente (textos em linhas
    separadas). `metadata.agrupadas` guarda cada original, para o histórico do
    CRM continuar mostrando as mensagens do paciente uma a uma."""
    if len(grupo) == 1:
        return grupo[0].mensagem
    ultima = grupo[-1].mensagem
    return ultima.model_copy(
        update={
            "texto": "\n".join(i.mensagem.texto or "" for i in grupo),
            "metadata": {
                **ultima.metadata,
                "agrupadas": [
                    {"message_id": i.mensagem.message_id, "texto": i.mensagem.texto or ""}
                    for i in grupo
                ],
            },
        }
    )


def _finalizar(client, grupo: list[ItemRajada], *, success: bool, error: str | None) -> None:
    for item in grupo:
        extra = {} if success else {"error": error}
        try:
            finish_event(
                client,
                provider=item.provider,
                event_id=item.event_id,
                success=success,
                **extra,
            )
        except Exception:
            logger.exception(
                "Falha ao finalizar evento de idempotência | provider=%s event_id=%s",
                item.provider,
                item.event_id,
            )


async def processar_lote_idempotente(itens: list[ItemRajada]) -> None:
    """Processa o lote de uma conversa (gateway/rajada.py), grupo a grupo, e
    grava o estado final de cada evento registrado no webhook."""
    if not itens:
        return
    client = get_supabase()
    for grupo in agrupar_lote(itens):
        primeiro = grupo[0]
        bind_tenant(primeiro.tenant.tenant_id)
        if len(grupo) > 1:
            logger.info(
                "Rajada agrupada numa resposta | tenant=%s | mensagens=%d",
                primeiro.tenant.tenant_id,
                len(grupo),
            )
        try:
            await processar_mensagem_entrada(
                combinar_mensagens(grupo), primeiro.tenant, primeiro.sender, primeiro.provider
            )
        except Exception as exc:
            metrics.increment("whatsapp_messages_failed", len(grupo))
            logger.exception(
                "Falha não tratada no evento | provider=%s event_id=%s tenant=%s",
                primeiro.provider,
                primeiro.event_id,
                primeiro.tenant.tenant_id,
            )
            _finalizar(client, grupo, success=False, error=str(exc))
            continue

        _finalizar(client, grupo, success=True, error=None)
        metrics.increment("whatsapp_messages_processed", len(grupo))
