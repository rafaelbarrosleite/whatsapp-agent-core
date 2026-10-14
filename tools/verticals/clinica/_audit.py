import logging

from supabase import Client

log = logging.getLogger(__name__)


async def log_integration(
    client: Client,
    tenant_id: str,
    integracao: str,
    operacao: str,
    request_payload: dict,
    response_status: int,
    response_body: str | None,
    duracao_ms: int,
    sucesso: bool,
    erro: str | None = None,
) -> None:
    """Grava log de chamada externa. Nunca propaga exceção."""
    try:
        client.table("integration_logs").insert(
            {
                "tenant_id": tenant_id,
                "integracao": integracao,
                "operacao": operacao,
                "request_payload": request_payload,
                "response_status": response_status,
                "response_body": response_body[:2000] if response_body else None,
                "duracao_ms": duracao_ms,
                "sucesso": sucesso,
                "erro": erro,
            }
        ).execute()
    except Exception as e:
        log.warning("Falha ao gravar integration_log: %s", e)
