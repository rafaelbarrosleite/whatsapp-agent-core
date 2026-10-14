import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from app.atendente_router import router as atendente_router
from app.config import settings
from app.config_router import router as config_router
from app.webhook_interno import router as router_interno
from core.error_notifier import WhatsAppErrorHandler
from core.observability import configure_structured_logging, correlation_middleware, metrics
from gateway.webhook_cloudapi import router as webhook_cloudapi_router
from security.token_compartilhado import token_valido


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger(__name__)

    # 1. Registrar tools
    from app.tools_setup import registrar_tools
    from core.registry import registry

    registrar_tools(registry)
    log.info("Tools de CRM e do vertical clínica registradas no registry.")

    # 2. Inicializar checkpointer e criar tabelas no banco do core (idempotente)
    from core.graph import criar_grafo
    from db.checkpointer import get_checkpointer

    checkpointer = await get_checkpointer()
    if hasattr(checkpointer, "setup"):
        await checkpointer.setup()
        log.info("AsyncPostgresSaver: tabelas do checkpointer verificadas/criadas.")
    else:
        log.info("Checkpointer: usando MemorySaver (SUPABASE_DB_URL não configurado).")

    # 3. Substituir o grafo do pipeline (compartilhado pelos dois webhooks)
    # pelo grafo com o checkpointer correto. FastAPI garante que o lifespan
    # termina antes de aceitar qualquer requisição.
    import core.pipeline as pipeline

    grafo = criar_grafo(checkpointer=checkpointer)
    pipeline._grafo = grafo
    log.info("Grafo inicializado com %s.", type(checkpointer).__name__)

    # 4. Registrar handler de alertas de erro via WhatsApp
    wh_handler = WhatsAppErrorHandler()
    wh_handler.setLevel(logging.ERROR)
    logging.getLogger().addHandler(wh_handler)
    log.info("WhatsApp error handler registrado.")

    # 5. Scheduler de jobs periódicos (agente — lembretes aos pacientes)
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from jobs.analisar_conversas import analisar_conversas
    from jobs.lembrete_consulta import enviar_lembretes_do_dia
    from jobs.reengajamento import reengajar_conversas_paradas

    scheduler = AsyncIOScheduler()
    # 12:00 UTC = 09:00 Brasília / 08:00 em UTC-4
    scheduler.add_job(enviar_lembretes_do_dia, "cron", hour=12, minute=0)
    # Reengajamento de leads parados ≥30min com o agente (one-shot; 08–20h no fuso da clínica)
    scheduler.add_job(reengajar_conversas_paradas, "interval", minutes=5)
    # IA lê conversas em atendimento humano → cria consulta/lead, move funil (nota interna)
    scheduler.add_job(analisar_conversas, "interval", minutes=10)
    scheduler.start()
    log.info(
        "Scheduler agente iniciado — lembretes 12:00 UTC, reengajamento 5min, análise IA 10min."
    )

    # 6. Internal Agent — grafo e scheduler do médico
    import agents.internal.agent as _internal_agent
    from agents.internal.agent import criar_grafo_interno
    from agents.internal.scheduler import start_scheduler as start_internal_scheduler

    grafo_interno = criar_grafo_interno(checkpointer=checkpointer)
    _internal_agent._grafo_interno = grafo_interno
    log.info("Internal Agent graph inicializado com %s.", type(checkpointer).__name__)

    internal_scheduler = start_internal_scheduler()
    internal_scheduler.start()
    log.info("Internal Agent scheduler iniciado — bom_dia 12:00 UTC, resumo 01:00 UTC.")

    yield

    scheduler.shutdown(wait=False)
    internal_scheduler.shutdown(wait=False)
    log.info("Schedulers encerrados.")


configure_structured_logging(settings.log_level)

app = FastAPI(title="IntelliAgents Core", version="0.1.0", lifespan=lifespan)
app.middleware("http")(correlation_middleware)
app.include_router(webhook_cloudapi_router)
app.include_router(router_interno)
app.include_router(atendente_router)
app.include_router(config_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def operational_metrics(authorization: str | None = Header(None)):
    """Contadores operacionais. Exige `Authorization: Bearer <INTERNAL_API_KEY>`;
    sem a chave configurada, responde 401 (falha fechado)."""
    esquema, _, recebido = (authorization or "").partition(" ")
    if esquema != "Bearer" or not token_valido(recebido or None, settings.internal_api_key):
        raise HTTPException(status_code=401, detail="Não autorizado")
    return metrics.snapshot()


@app.post("/admin/coexistencia/sincronizar")
async def admin_sincronizar_coexistencia(
    body: dict, authorization: str | None = Header(None)
) -> dict:
    """Pede à Meta o histórico ou a agenda do app WhatsApp Business de um número
    em coexistência. Body: {"numero": "+55...", "tipo": "history" |
    "smb_app_state_sync"}. A Meta só aceita o histórico nas 24 h seguintes à
    conexão do número. Exige `Authorization: Bearer <INTERNAL_API_KEY>`."""
    esquema, _, recebido = (authorization or "").partition(" ")
    if esquema != "Bearer" or not token_valido(recebido or None, settings.internal_api_key):
        raise HTTPException(status_code=401, detail="Não autorizado")
    numero = str(body.get("numero") or "").strip()
    tipo = str(body.get("tipo") or "").strip()
    if not numero or tipo not in ("history", "smb_app_state_sync"):
        raise HTTPException(
            status_code=400,
            detail="Informe numero e tipo ('history' ou 'smb_app_state_sync')",
        )

    from channels.credenciais import CredencialCanalAusenteError
    from channels.sender_factory import criar_sender
    from gateway.tenant_resolver import TenantNaoEncontradoError, TenantResolver

    try:
        tenant = TenantResolver().resolver(numero)
        sender = criar_sender(
            tenant.provider,
            tenant.credenciais_ref,
            tenant_id=tenant.tenant_id,
            phone_number_id=tenant.phone_number_id,
            token_cifrado=tenant.token_cifrado,
        )
    except TenantNaoEncontradoError:
        raise HTTPException(status_code=404, detail="Número sem canal ativo") from None
    except CredencialCanalAusenteError:
        raise HTTPException(status_code=409, detail="Canal sem credencial") from None

    aceito, detalhe = await sender.solicitar_sincronizacao_app(tipo)
    return {"aceito": aceito, "tipo": tipo, "meta": detalhe}
