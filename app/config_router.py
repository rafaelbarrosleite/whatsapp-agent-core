"""app/config_router.py — endpoint chamado pelo painel CRM (Next.js, repositório separado)
para atualizar configurações por tenant (tenants.config no banco CORE).

Não confundir com a tabela tenant_config (credenciais de
integrações externas) — aqui é a coluna tenants.config, a mesma lida por
core/nodes.carregar_tenant e usada pelas feature flags handoff/agenda.
"""

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from db.client import get_supabase
from security.token_compartilhado import token_valido

log = logging.getLogger(__name__)
router = APIRouter()

# Nunca aceitar chaves arbitrárias do CRM — só estas podem ser escritas em
# tenants.config. Adicionar aqui explicitamente ao criar uma nova feature flag.
CHAVES_PERMITIDAS = {"agenda.habilitada", "agenda.pedido_pela_bia", "handoff.habilitado"}


class TenantConfigBody(BaseModel):
    chave: str
    valor: bool
    tenant_id: str


def _verificar_auth(authorization: str | None) -> None:
    # Tempo constante (token_valido): `!=` deixaria medir a chave pelo tempo.
    esquema, _, recebido = (authorization or "").partition(" ")
    if esquema != "Bearer" or not token_valido(recebido or None, settings.internal_api_key):
        raise HTTPException(status_code=401, detail="Não autorizado")


@router.patch("/api/tenant/config")
async def atualizar_config(
    body: TenantConfigBody,
    authorization: str | None = Header(None),
) -> dict:
    _verificar_auth(authorization)

    if body.chave not in CHAVES_PERMITIDAS:
        raise HTTPException(status_code=400, detail=f"Chave '{body.chave}' não é permitida")

    core = get_supabase()
    resultado = (
        core.table("tenants").select("id, config").eq("id", body.tenant_id).limit(1).execute()
    )
    if not resultado.data:
        raise HTTPException(status_code=404, detail="Tenant não encontrado")

    config_atual = resultado.data[0].get("config") or {}
    secao, subchave = body.chave.split(".", 1)
    config_novo = {
        **config_atual,
        secao: {**(config_atual.get(secao) or {}), subchave: body.valor},
    }

    atualizado = (
        core.table("tenants").update({"config": config_novo}).eq("id", body.tenant_id).execute()
    )
    if not atualizado.data:
        raise HTTPException(status_code=404, detail="Tenant não encontrado")

    return {"sucesso": True, "config": config_novo}
