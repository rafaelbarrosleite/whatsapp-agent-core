"""agents/internal/tenant_resolver.py — Resolução de tenant pelo número do médico.

Dado o número WhatsApp do remetente de uma mensagem no canal interno,
resolve qual tenant corresponde a esse médico consultando o Core DB.
"""

import logging

from channels.base import _normalizar_numero_br
from db.client import get_supabase

log = logging.getLogger(__name__)


async def resolver_tenant_por_medico(numero: str) -> dict | None:
    """Resolve o tenant dado o número WhatsApp do médico remetente.

    Normaliza o número antes de comparar (Meta envia sem '+', config tem com '+').
    Busca todos os tenants ativos e compara config->>'medico_whatsapp' normalizado
    em Python (PostgREST não suporta normalização no servidor).

    Retorna None se:
    - Nenhum tenant ativo tem esse número como medico_whatsapp
    - Colisão: 2+ tenants com o mesmo número (estado inválido → log.error)
    - Tenant inativo (filtrado no banco)

    Retorna dict {id, nome, config} do único tenant correspondente.
    """
    numero_normalizado = _normalizar_numero_br(numero)

    resultado = (
        get_supabase().table("tenants").select("id, nome, config").eq("ativo", True).execute()
    )
    tenants_ativos = resultado.data or []

    correspondentes = [
        t
        for t in tenants_ativos
        if _normalizar_numero_br(((t.get("config") or {}).get("medico_whatsapp") or ""))
        == numero_normalizado
    ]

    if not correspondentes:
        return None

    if len(correspondentes) > 1:
        ids = [t["id"] for t in correspondentes]
        log.error(
            "COLISÃO: múltiplos tenants com mesmo medico_whatsapp=%s | tenant_ids=%s",
            numero_normalizado,
            ids,
        )
        return None

    return correspondentes[0]
