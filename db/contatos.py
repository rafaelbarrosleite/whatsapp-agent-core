import logging

from supabase import Client

log = logging.getLogger(__name__)


def promover_para_paciente(client: Client, contato_id: str, tenant_id: str) -> None:
    """Atualiza tipo do contato de 'lead' para 'paciente'.

    Idempotente — se já for 'paciente' o UPDATE não muda nada.
    Falha silenciosa: erro logado mas não relançado para não bloquear o fluxo do agendamento.
    """
    try:
        client.table("contatos").update({"tipo": "paciente"}).eq("id", contato_id).eq(
            "tenant_id", tenant_id
        ).execute()
    except Exception:
        log.exception(
            "Erro ao promover contato para paciente | contato_id=%s tenant_id=%s",
            contato_id,
            tenant_id,
        )
