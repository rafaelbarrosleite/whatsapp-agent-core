"""gateway/eco_celular.py — coexistência: mensagem que a clínica mandou pelo
app WhatsApp Business no celular (webhook smb_message_echoes).

A clínica respondeu o paciente pelo celular → o agente pausa nessa conversa (para
não responder por cima) e a mensagem entra no histórico do CRM. Mesmo efeito de
a atendente assumir pelo painel; o agente volta quando alguém devolver no CRM.
"""

import logging

from channels.base import EcoMensagemClinica
from core.observability import bind_tenant
from db.client import get_supabase
from db.conversas import registrar_mensagem_celular_clinica
from db.handoff import mover_lead_para_com_atendente
from db.idempotency import finish_event
from db.vertical_client import get_vertical_supabase
from gateway.tenant_resolver import TenantInfo

logger = logging.getLogger(__name__)

PROVIDER_ECO = "cloud_api_eco"


async def processar_eco_celular(
    *, eco: EcoMensagemClinica, tenant: TenantInfo, event_id: str
) -> None:
    client = get_supabase()
    bind_tenant(tenant.tenant_id)
    try:
        contato_id, conversa_id = registrar_mensagem_celular_clinica(
            client,
            tenant.tenant_id,
            eco.numero_cliente,
            eco.texto,
            eco.message_id,
            tenant.canal_id,
        )
    except Exception as exc:
        logger.exception(
            "Falha ao registrar mensagem da clínica pelo celular | tenant=%s | message_id=%s",
            tenant.tenant_id,
            eco.message_id,
        )
        finish_event(
            client, provider=PROVIDER_ECO, event_id=event_id, success=False, error=str(exc)
        )
        return

    # Humano no controle → lead vai para "com_atendente" no funil (best-effort).
    mover_lead_para_com_atendente(get_vertical_supabase(), tenant.tenant_id, contato_id)
    finish_event(client, provider=PROVIDER_ECO, event_id=event_id, success=True)
    # Só metadados: o conteúdo pode ser dado de saúde (LGPD).
    logger.info(
        "Clínica respondeu pelo celular — agente pausado | tenant=%s | conversa=%s | tipo=%s",
        tenant.tenant_id,
        conversa_id,
        eco.tipo,
    )
