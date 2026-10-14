#!/usr/bin/env python3
"""
Seed de tenant para desenvolvimento local.
Cria um tenant de exemplo do vertical "clinica" com system_prompt simples.

Executar: uv run python scripts/seed_tenant_teste.py

Não usar em produção. Para remover o tenant, copie o tenant_id e execute:
  DELETE FROM tenants WHERE id = '<tenant_id>';  (CASCADE remove tudo)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.client import get_supabase


def main() -> None:
    client = get_supabase()

    resultado = (
        client.table("tenants")
        .insert(
            {
                "nome": "Clínica Exemplo (DEV)",
                "vertical": "clinica",
                "system_prompt": (
                    "Você é a Ana, assistente de atendimento da Clínica Exemplo. "
                    "Seja cordial e objetiva. Responda dúvidas com a base de conhecimento, "
                    "ofereça horários e marque consultas usando as tools de agenda. "
                    "Nunca invente informações médicas, preços ou horários."
                ),
                "config": {
                    "nome_agente": "Ana",
                    "fuso_utc_horas": -3,
                    # Liga a agenda de demonstração (tools/verticals/clinica/agenda_demo.py).
                    "agenda": {"habilitada": True},
                },
            }
        )
        .execute()
    )

    tenant = resultado.data[0]
    tenant_id = tenant["id"]

    print("\n  Tenant criado")
    print(f"  {'─' * 48}")
    print(f"  tenant_id : {tenant_id}")
    print(f"  nome      : {tenant['nome']}")
    print(f"  vertical  : {tenant['vertical']}")
    print("\n  Para conversar com o agente (escolha qualquer telefone de teste):")
    print(f"\n    uv run python scripts/chat_cli.py {tenant_id} +5511999990001\n")


if __name__ == "__main__":
    main()
