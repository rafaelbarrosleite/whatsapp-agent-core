#!/usr/bin/env python3
"""
Smoke test com Supabase REAL.

Valida: conexão, service_role key, isolamento de tenant, tools de CRM.
Limpa tudo que criou ao final (mesmo se falhar).

Pré-requisito: .env com SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY reais.
Executar: uv run python scripts/smoke_supabase.py
"""

import sys
from pathlib import Path

# Permite importar os módulos do projeto independente de onde o script é chamado
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.client import get_supabase
from tools.crm.contatos import _make_buscar_contato, _make_criar_contato

TELEFONE_TESTE = "+5511999990001"
NOME_TESTE = "Contato Smoke Test"


def linha(titulo: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {titulo}")
    print("─" * 55)


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def main() -> None:
    client = get_supabase()
    tenant_id: str | None = None

    try:
        # ------------------------------------------------------------------
        # 1. Criar tenant de teste diretamente no banco
        # ------------------------------------------------------------------
        linha("1. Criando tenant de teste")
        res = (
            client.table("tenants")
            .insert(
                {
                    "nome": "Clinica Smoke Test",
                    "vertical": "clinica",
                    "system_prompt": "Agente de teste — pode apagar.",
                }
            )
            .execute()
        )
        tenant = res.data[0]
        tenant_id = tenant["id"]
        ok(f"tenant criado  | id={tenant_id}  vertical={tenant['vertical']}")

        # ------------------------------------------------------------------
        # 2. Criar contato via tool (com tenant_id lacrado na closure)
        # ------------------------------------------------------------------
        linha("2. Criando contato via tool criar_contato")
        criar = _make_criar_contato(client)(tenant_id)
        contato = criar.invoke({"telefone": TELEFONE_TESTE, "nome": NOME_TESTE})
        ok(f"contato criado | id={contato['id']}")
        ok(f"telefone       | {contato['telefone']}")
        ok(f"tenant_id salvo| {contato['tenant_id']}")

        assert contato["tenant_id"] == tenant_id, "tenant_id no banco nao bate!"
        assert contato["telefone"] == TELEFONE_TESTE

        # ------------------------------------------------------------------
        # 3. Buscar contato via tool
        # ------------------------------------------------------------------
        linha("3. Buscando contato via tool buscar_contato")
        buscar = _make_buscar_contato(client)(tenant_id)
        encontrado = buscar.invoke({"telefone": TELEFONE_TESTE})
        ok(f"contato encontrado: {encontrado}")

        assert encontrado is not None, "buscar_contato retornou None!"
        assert encontrado["telefone"] == TELEFONE_TESTE
        assert encontrado["nome"] == NOME_TESTE
        # tenant_id nao deve aparecer no retorno da tool (nao foi selecionado)
        assert "tenant_id" not in encontrado, "tenant_id vazou no retorno da tool!"
        ok("tenant_id ausente no retorno da tool (correto — nao exposto ao LLM)")

        # ------------------------------------------------------------------
        # 4. Confirmar isolamento: buscar com tenant errado retorna None
        # ------------------------------------------------------------------
        linha("4. Validando isolamento de tenant")
        tenant_errado = "00000000-0000-0000-0000-000000000000"
        buscar_errado = _make_buscar_contato(client)(tenant_errado)
        resultado_errado = buscar_errado.invoke({"telefone": TELEFONE_TESTE})
        assert resultado_errado is None, "VAZAMENTO DE DADOS: outro tenant encontrou o contato!"
        ok("busca com tenant errado retornou None (isolamento OK)")

        # ------------------------------------------------------------------
        # Resultado final
        # ------------------------------------------------------------------
        linha("RESULTADO")
        print("\n  SMOKE TEST PASSOU — conexao, tools e isolamento validados.\n")

    except Exception as exc:
        linha("RESULTADO")
        print(f"\n  SMOKE TEST FALHOU: {exc}\n")
        raise

    finally:
        # Apaga o tenant de teste; CASCADE remove contatos, conversas, etc.
        if tenant_id:
            linha("Limpeza")
            client.table("tenants").delete().eq("id", tenant_id).execute()
            ok(f"tenant {tenant_id} removido (CASCADE apagou os dados filhos)")


if __name__ == "__main__":
    main()
