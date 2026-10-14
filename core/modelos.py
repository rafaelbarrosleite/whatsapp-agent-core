"""core/modelos.py — modelo Claude por clínica.

A área administrativa do CRM grava em `tenants.config.modelos_agente`:
    {"atendimento": "<id>", "interno": "<id>"}
Só entram modelos validados com o motor atual (LangChain + tool use). Valor
ausente ou fora da lista cai no padrão, para nunca derrubar o atendimento.
"""

from typing import Literal

Uso = Literal["atendimento", "interno"]

# Validados com o motor: tool use via bind_tools, sem thinking explícito.
MODELOS_PERMITIDOS: frozenset[str] = frozenset({"claude-sonnet-4-6", "claude-haiku-4-5"})

PADRAO: dict[Uso, str] = {"atendimento": "claude-sonnet-4-6", "interno": "claude-sonnet-4-6"}


def modelo_do_tenant(tenant_config: dict | None, uso: Uso) -> str:
    """Modelo configurado para a clínica, ou o padrão."""
    escolhido = ((tenant_config or {}).get("modelos_agente") or {}).get(uso)
    return escolhido if escolhido in MODELOS_PERMITIDOS else PADRAO[uso]
