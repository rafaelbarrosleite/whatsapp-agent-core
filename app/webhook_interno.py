"""app/webhook_interno.py — Webhook do canal interno (médico → Internal Agent).

Recebe mensagens do médico via WhatsApp Cloud API e roteia para o Internal Agent.
NÃO misturar com o agente (agente paciente) — canais completamente separados.

Multi-tenant: o tenant é resolvido dinamicamente pelo número remetente via DB.
Thread por tenant: f"medico:{tenant_id}" — histórico isolado por médico.
Identidade: tenant_id e tenant_config propagados via config["configurable"].

Canal de envio: channels.dispatcher.enviar_mensagem_tenant resolve provider e
credenciais a partir do canal ativo do tenant em canais_whatsapp — mesmo canal
usado para o paciente. A separação paciente/médico é garantida pelo DESTINO
(medico_whatsapp do tenant), não por um canal de envio diferente.

Segurança:
- GET: verificação de token Meta (mesmo padrão do webhook agente)
- POST: validação HMAC X-Hub-Signature-256 via WHATSAPP_APP_SECRET
- POST: barreira secundária — resolver_tenant_por_medico rejeita remetente desconhecido
"""

import json
import logging
from collections import deque

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse
from langchain_core.messages import AIMessage, HumanMessage

from agents.internal.agent import RECURSION_LIMIT, _grafo_interno
from agents.internal.tenant_resolver import resolver_tenant_por_medico
from app.config import settings
from channels.cloud_api import CloudApiAdapter
from channels.dispatcher import enviar_mensagem_tenant
from db.client import get_supabase
from security.hmac_meta import validar_assinatura_meta_algum
from security.token_compartilhado import token_valido

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = CloudApiAdapter()

# Deduplicação in-memory por message_id (mesmo padrão do webhook agente)
_MAX_DEDUP = 500
_dedup_ids: set[str] = set()
_dedup_fila: deque[str] = deque()


async def _processar_mensagem_medico(texto: str, tenant: dict) -> None:
    """Invoca o Internal Agent e envia a resposta ao médico via WhatsApp.

    Chamada como BackgroundTask — o webhook já retornou 200 antes desta função rodar.
    Qualquer exceção é logada mas não propaga (não afeta o status HTTP já entregue).
    """
    tenant_id = tenant["id"]
    tenant_config = tenant.get("config") or {}
    thread_id = f"medico:{tenant_id}"

    langgraph_config = {
        "configurable": {
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "tenant_config": tenant_config,
        },
        "recursion_limit": RECURSION_LIMIT,
    }

    try:
        # Primeiro turno: fornece estado completo; turnos seguintes: só a nova mensagem.
        # Checkpointer restaura tenant_id e histórico dos turnos anteriores.
        primeiro_turno = not bool((await _grafo_interno.aget_state(langgraph_config)).values)
        if primeiro_turno:
            entrada = {
                "messages": [HumanMessage(content=texto)],
                "tenant_id": tenant_id,
            }
        else:
            entrada = {"messages": [HumanMessage(content=texto)]}

        resultado = await _grafo_interno.ainvoke(entrada, config=langgraph_config)

        ultima_ai = next(
            (m for m in reversed(resultado["messages"]) if isinstance(m, AIMessage)),
            None,
        )
        if ultima_ai is None or not ultima_ai.content:
            log.warning("Internal Agent não gerou resposta | thread=%s", thread_id)
            return

        content = ultima_ai.content
        if isinstance(content, list):
            resposta_texto = " ".join(
                bloco.get("text", "")
                for bloco in content
                if isinstance(bloco, dict) and bloco.get("type") == "text"
            )
        else:
            resposta_texto = str(content)

        if not resposta_texto.strip():
            log.warning("Resposta do Internal Agent vazia após extração | thread=%s", thread_id)
            return

        log.info(
            "Resposta gerada | thread=%s | chars=%d",
            thread_id,
            len(resposta_texto),
        )

        medico_whatsapp = tenant_config.get("medico_whatsapp") or ""
        enviado = await enviar_mensagem_tenant(
            tenant_id=tenant_id,
            numero_destino=medico_whatsapp,
            texto=resposta_texto,
            core_db=get_supabase(),
        )
        if not enviado:
            log.error(
                "Falha ao enviar resposta ao médico | thread=%s | tenant=%s", thread_id, tenant_id
            )

    except Exception:
        log.exception(
            "Erro no processamento do Internal Agent | thread=%s | tenant=%s", thread_id, tenant_id
        )


@router.get("/webhook/interno")
async def verificar_webhook_interno(request: Request) -> PlainTextResponse:
    """Verificação de webhook exigida pela Meta antes de ativar o endpoint."""
    params = request.query_params
    mode = params.get("hub.mode")
    verify_token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    token_ok = token_valido(verify_token, settings.whatsapp_verify_token)
    if mode == "subscribe" and token_ok:
        log.info("Webhook interno verificado com sucesso.")
        return PlainTextResponse(content=challenge, status_code=200)

    log.warning(
        "Verificação do webhook interno rejeitada | mode=%s | token_ok=%s",
        mode,
        token_ok,
    )
    return PlainTextResponse(content="Forbidden", status_code=403)


@router.post("/webhook/interno")
async def webhook_interno(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Recebe mensagens do canal interno e despacha para o Internal Agent.

    Sempre retorna 200 para a Meta após validação — erros são tratados internamente.
    """
    raw_body = await request.body()

    app_secrets = settings.whatsapp_app_secrets
    if app_secrets:
        signature = request.headers.get("x-hub-signature-256", "")
        ip = request.client.host if request.client else "desconhecido"
        if not validar_assinatura_meta_algum(raw_body, signature, app_secrets):
            log.warning(
                "Webhook interno rejeitado — assinatura inválida | ip=%s | sig_presente=%s",
                ip,
                bool(signature),
            )
            return PlainTextResponse(content="Unauthorized", status_code=401)
    else:
        log.error(
            "WHATSAPP_APP_SECRET não configurado — impossível validar webhook interno Cloud API. "
            "Configure a variável antes de expor este endpoint ao tráfego real."
        )
        return PlainTextResponse(content="Internal Server Error", status_code=500)

    try:
        payload = json.loads(raw_body)
    except Exception:
        log.warning("Payload do webhook interno não é JSON válido — ignorando")
        return {"status": "ok"}

    if not _adapter.eh_mensagem_entrada(payload):
        return {"status": "ignorado"}

    try:
        mensagem = _adapter.normalizar(payload)
    except Exception:
        log.warning("Falha ao normalizar payload do webhook interno — ignorando")
        return {"status": "ok"}

    # Deduplicação in-memory
    if mensagem.message_id in _dedup_ids:
        log.info("Mensagem duplicada ignorada | message_id=%s", mensagem.message_id)
        return {"status": "ok"}
    _dedup_ids.add(mensagem.message_id)
    _dedup_fila.append(mensagem.message_id)
    if len(_dedup_fila) > _MAX_DEDUP:
        _dedup_ids.discard(_dedup_fila.popleft())

    # Resolução de tenant — identifica o médico pelo número remetente
    tenant = await resolver_tenant_por_medico(mensagem.numero_remetente)
    if tenant is None:
        log.warning(
            "Mensagem ignorada — remetente não autorizado ou sem tenant ativo | de=%s",
            mensagem.numero_remetente,
        )
        return {"status": "ok"}

    if not mensagem.texto:
        log.info(
            "Mensagem sem texto ignorada (mídia não suportada) | tipo=%s | tenant=%s",
            mensagem.tipo,
            tenant["id"],
        )
        return {"status": "ok"}

    log.info(
        "Mensagem do médico recebida | de=%s | tenant=%s | message_id=%s | chars=%d",
        mensagem.numero_remetente,
        tenant["id"],
        mensagem.message_id,
        len(mensagem.texto or ""),
    )

    background_tasks.add_task(_processar_mensagem_medico, mensagem.texto, tenant)
    return {"status": "ok"}
