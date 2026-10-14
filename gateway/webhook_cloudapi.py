import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from channels.base import ContatoAgenda, ConversaHistorico, TipoMensagem
from channels.cloud_api import CloudApiAdapter
from channels.credenciais import CredencialCanalAusenteError
from channels.sender_factory import criar_sender
from core.observability import bind_tenant, metrics
from db.client import get_supabase
from db.idempotency import claim_event, external_event_id, payload_sha256
from gateway import rajada
from gateway.eco_celular import PROVIDER_ECO, processar_eco_celular
from gateway.historico_coexistencia import PROVIDER_HISTORICO, processar_sincronizacao_app
from gateway.idempotent_processing import processar_lote_idempotente
from gateway.tenant_resolver import TenantInfo, TenantNaoEncontradoError, TenantResolver
from security.hmac_meta import validar_assinatura_meta_algum
from security.token_compartilhado import token_valido

logger = logging.getLogger(__name__)
router = APIRouter()

_adapter = CloudApiAdapter()
_TIPOS_COM_SUPORTE = {TipoMensagem.audio, TipoMensagem.imagem, TipoMensagem.documento}
_resolver: TenantResolver | None = None


def _get_resolver() -> TenantResolver:
    global _resolver
    if _resolver is None:
        _resolver = TenantResolver()
    return _resolver


@router.get("/webhook/cloudapi")
async def verificar_webhook(request: Request) -> PlainTextResponse:
    """Verificação de webhook exigida pela Meta antes de ativar o endpoint."""
    params = request.query_params
    mode = params.get("hub.mode")
    verify_token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token_valido(verify_token, settings.whatsapp_verify_token):
        logger.info("Webhook Cloud API verificado com sucesso.")
        return PlainTextResponse(content=challenge, status_code=200)

    logger.warning("Verificação de webhook rejeitada | mode=%s | token_ok=%s", mode, False)
    return PlainTextResponse(content="Forbidden", status_code=403)


@router.post("/webhook/cloudapi")
async def webhook_cloudapi(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw_body = await request.body()

    app_secrets = settings.whatsapp_app_secrets
    if app_secrets:
        signature = request.headers.get("x-hub-signature-256", "")
        ip = request.client.host if request.client else "desconhecido"
        if not validar_assinatura_meta_algum(raw_body, signature, app_secrets):
            logger.warning(
                "Webhook Cloud API rejeitado — assinatura inválida | ip=%s | sig_presente=%s",
                ip,
                bool(signature),
            )
            return PlainTextResponse(content="Unauthorized", status_code=401)
    else:
        logger.error(
            "WHATSAPP_APP_SECRET não configurado — impossível validar webhook Cloud API. "
            "Configure a variável antes de expor este endpoint ao tráfego real."
        )
        return PlainTextResponse(content="Internal Server Error", status_code=500)

    try:
        payload = json.loads(raw_body)
    except Exception:
        logger.warning("Payload do webhook Cloud API não é JSON válido — ignorando")
        return {"status": "ignorado"}
    if not isinstance(payload, dict):
        return {"status": "ignorado"}

    # Um POST da Meta pode trazer eventos de tipos diferentes (várias entry e
    # changes). Cada tipo é tratado; nenhum interrompe os outros.
    pedir_retry = False
    tratou_algo = False

    if _adapter.eh_eco_celular(payload):
        tratou_algo = True
        pedir_retry |= not _despachar_ecos_celular(payload, raw_body, background_tasks)

    if _adapter.eh_sincronizacao_app(payload):
        tratou_algo = True
        pedir_retry |= not _despachar_sincronizacao_app(payload, raw_body, background_tasks)

    resultado: dict = {"status": "ok"}
    if _adapter.eh_mensagem_entrada(payload):
        tratou_algo = True
        ok, duplicadas = _despachar_mensagens(payload, raw_body, background_tasks)
        pedir_retry |= not ok
        if duplicadas:
            resultado["duplicate"] = True

    if pedir_retry:
        # 503 → a Meta reenvia o POST inteiro. O que já foi registrado nesta
        # tentativa é reconhecido como duplicado e não roda de novo.
        return PlainTextResponse(content="Service Unavailable", status_code=503)
    if not tratou_algo:
        return {"status": "ignorado"}
    return resultado


def _despachar_mensagens(
    payload: dict, raw_body: bytes, background_tasks: BackgroundTasks
) -> tuple[bool, bool]:
    """Registra e enfileira TODAS as mensagens do payload.

    Retorna (ok, todas_duplicadas). ok=False quando o registro de idempotência
    falhou para alguma mensagem (a Meta deve reenviar).
    """
    mensagens = _adapter.normalizar_todas(payload)
    ok = True
    novas = 0
    duplicadas = 0
    chaves_novas: list[str] = []
    contexto_por_numero: dict[str, tuple[TenantInfo, object] | None] = {}

    for mensagem in mensagens:
        metrics.increment("whatsapp_messages_received")

        if mensagem.numero_destino not in contexto_por_numero:
            contexto_por_numero[mensagem.numero_destino] = _resolver_contexto(mensagem)
        contexto = contexto_por_numero[mensagem.numero_destino]
        if contexto is None:
            metrics.increment("whatsapp_messages_failed")
            continue
        tenant, sender = contexto
        bind_tenant(tenant.tenant_id)

        if not mensagem.texto and mensagem.tipo not in _TIPOS_COM_SUPORTE:
            logger.info(
                "Mensagem sem texto ignorada (mídia não suportada) | tipo=%s | de=%s | tenant=%s",
                mensagem.tipo,
                mensagem.numero_remetente,
                tenant.tenant_id,
            )
            continue

        event_id = external_event_id(mensagem.message_id, raw_body)
        try:
            claimed = claim_event(
                get_supabase(),
                provider="cloud_api",
                event_id=event_id,
                tenant_id=tenant.tenant_id,
                raw_body=raw_body,
            )
        except Exception:
            metrics.increment("whatsapp_messages_failed")
            logger.exception(
                "Falha ao registrar idempotência — solicitando retry | event_id=%s tenant=%s",
                event_id,
                tenant.tenant_id,
            )
            ok = False
            continue
        if not claimed:
            duplicadas += 1
            metrics.increment("whatsapp_messages_duplicate")
            logger.info("Evento duplicado ignorado | provider=cloud_api event_id=%s", event_id)
            continue

        novas += 1
        # Nunca logar o conteúdo da mensagem (dado de saúde / LGPD) — só metadados.
        logger.info(
            "Mensagem recebida | tenant=%s | vertical=%s | de=%s | tipo=%s | message_id=%s"
            " | chars=%d",
            tenant.tenant_id,
            tenant.vertical,
            mensagem.numero_remetente,
            mensagem.tipo,
            mensagem.message_id,
            len(mensagem.texto or ""),
        )
        chave = rajada.adicionar(
            rajada.ItemRajada(
                event_id=event_id,
                mensagem=mensagem,
                tenant=tenant,
                sender=sender,
                provider="cloud_api",
            )
        )
        if chave is not None:
            chaves_novas.append(chave)

    if chaves_novas:
        background_tasks.add_task(rajada.drenar_varias, chaves_novas, processar_lote_idempotente)
    return ok, duplicadas > 0 and novas == 0


def _resolver_contexto(mensagem) -> tuple[TenantInfo, object] | None:
    """Tenant + sender do número que recebeu. None = não processar (logado)."""
    try:
        tenant = _get_resolver().resolver(mensagem.numero_destino)
    except TenantNaoEncontradoError:
        logger.warning(
            "Número não cadastrado | numero=%s | message_id=%s",
            mensagem.numero_destino,
            mensagem.message_id,
        )
        return None

    # Credencial do canal da clínica (item 1.8, sem fallback global). Antes do
    # registro de idempotência: sem credencial não há como responder, e a falha
    # vira alerta (log de erro) em vez de resposta pelo número de outra clínica.
    try:
        sender = criar_sender(
            tenant.provider,
            tenant.credenciais_ref,
            tenant_id=tenant.tenant_id,
            phone_number_id=tenant.phone_number_id,
            token_cifrado=tenant.token_cifrado,
        )
    except CredencialCanalAusenteError:
        logger.error(
            "Canal sem credencial — mensagem não processada | tenant=%s | canal=%s | message_id=%s",
            tenant.tenant_id,
            tenant.canal_id,
            mensagem.message_id,
        )
        return None
    return tenant, sender


def _despachar_sincronizacao_app(
    payload: dict, raw_body: bytes, background_tasks: BackgroundTasks
) -> bool:
    """Coexistência: histórico (`history`) e agenda (`smb_app_state_sync`) do
    app da clínica. Retorna False se a Meta deve reenviar."""
    conversas, erros = _adapter.normalizar_historico(payload)
    contatos = _adapter.normalizar_agenda(payload)
    for erro in erros:
        logger.warning("Meta recusou o histórico do app | erro=%s", erro)

    por_numero: dict[str, tuple[list[ConversaHistorico], list[ContatoAgenda]]] = {}
    for conversa in conversas:
        por_numero.setdefault(conversa.numero_negocio, ([], []))[0].append(conversa)
    for contato in contatos:
        por_numero.setdefault(contato.numero_negocio, ([], []))[1].append(contato)

    for numero, (conversas_n, contatos_n) in por_numero.items():
        try:
            tenant = _get_resolver().resolver(numero)
        except TenantNaoEncontradoError:
            logger.warning("Histórico do app para número não cadastrado | numero=%s", numero)
            continue
        event_id = f"{numero}:sha256:{payload_sha256(raw_body)}"
        try:
            claimed = claim_event(
                get_supabase(),
                provider=PROVIDER_HISTORICO,
                event_id=event_id,
                tenant_id=tenant.tenant_id,
                raw_body=raw_body,
            )
        except Exception:
            logger.exception(
                "Falha ao registrar idempotência do histórico — solicitando retry | tenant=%s",
                tenant.tenant_id,
            )
            return False
        if not claimed:
            continue
        background_tasks.add_task(
            processar_sincronizacao_app,
            tenant=tenant,
            conversas=conversas_n,
            contatos=contatos_n,
            event_id=event_id,
        )
    return True


def _despachar_ecos_celular(
    payload: dict, raw_body: bytes, background_tasks: BackgroundTasks
) -> bool:
    """Coexistência: a clínica respondeu pelo celular → pausa o agente na conversa.
    Retorna False se a Meta deve reenviar."""
    for eco in _adapter.normalizar_ecos(payload):
        try:
            tenant = _get_resolver().resolver(eco.numero_negocio)
        except TenantNaoEncontradoError:
            logger.warning(
                "Eco do celular para número não cadastrado | numero=%s | message_id=%s",
                eco.numero_negocio,
                eco.message_id,
            )
            continue
        try:
            claimed = claim_event(
                get_supabase(),
                provider=PROVIDER_ECO,
                event_id=eco.message_id,
                tenant_id=tenant.tenant_id,
                raw_body=raw_body,
            )
        except Exception:
            logger.exception(
                "Falha ao registrar idempotência do eco — solicitando retry | tenant=%s",
                tenant.tenant_id,
            )
            return False
        if not claimed:
            continue
        background_tasks.add_task(
            processar_eco_celular, eco=eco, tenant=tenant, event_id=eco.message_id
        )
    return True
