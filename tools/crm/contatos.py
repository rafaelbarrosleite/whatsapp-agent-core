from langchain_core.tools import BaseTool, tool

from core.registry import ToolFactory, ToolRegistry
from core.telefone import normalizar_telefone
from db.client import get_supabase
from supabase import Client


def _make_buscar_contato(client: Client) -> ToolFactory:
    """Retorna uma factory que cria a tool buscar_contato com o cliente injetado."""

    def factory(tenant_id: str) -> BaseTool:
        # tenant_id fica lacrado nesta closure — o LLM só vê `telefone`.
        @tool
        def buscar_contato(telefone: str) -> dict | None:
            """Busca um contato pelo número de telefone.
            Retorna os dados do contato ou None se não encontrado."""
            # Normaliza o número que o LLM passou (pode vir "67 99999-1234",
            # sem DDI, etc.) para a MESMA forma canônica com que foi gravado —
            # senão o match exato falha e o LLM acaba criando um duplicado.
            telefone_canonico = normalizar_telefone(telefone)
            result = (
                client.table("contatos")
                .select("id, nome, telefone, metadata, created_at")
                .eq("tenant_id", tenant_id)  # filtro forçado — nunca omitir
                .eq("telefone", telefone_canonico)
                .execute()
            )
            return result.data[0] if result.data else None

        return buscar_contato

    return factory


def _make_criar_contato(client: Client) -> ToolFactory:
    """Retorna uma factory que cria a tool criar_contato com o cliente injetado."""

    def factory(tenant_id: str) -> BaseTool:
        @tool
        def criar_contato(telefone: str, nome: str | None = None) -> dict:
            """Cria um novo contato com o número de telefone informado.
            Use somente se buscar_contato não encontrar o contato."""
            # Grava sempre na forma canônica — a UNIQUE (tenant_id, telefone)
            # no banco depende disso para barrar duplicatas.
            telefone_canonico = normalizar_telefone(telefone)
            result = (
                client.table("contatos")
                .insert(
                    {
                        "tenant_id": tenant_id,
                        "telefone": telefone_canonico,
                        "nome": nome,
                        "tipo": "lead",
                    }
                )
                .execute()
            )
            return result.data[0]

        return criar_contato

    return factory


def registrar(registry: ToolRegistry, client: Client | None = None) -> None:
    """Registra as tools de CRM como genéricas (valem para todos os verticais).

    Aceita um client externo para facilitar testes sem conexão real ao Supabase.
    """
    _client = client or get_supabase()
    registry.register_generic(_make_buscar_contato(_client))
    registry.register_generic(_make_criar_contato(_client))
