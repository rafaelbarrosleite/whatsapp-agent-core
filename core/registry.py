import logging
from collections.abc import Callable

from langchain_core.tools import BaseTool

# Uma factory recebe tenant_id e devolve uma tool com esse tenant_id lacrado na closure.
# O LLM nunca vê tenant_id — ele não aparece no schema da tool.
ToolFactory = Callable[[str], BaseTool]


class ToolRegistry:
    def __init__(self) -> None:
        self._generic: list[ToolFactory] = []
        self._by_vertical: dict[str, list[ToolFactory]] = {}

    def register_generic(self, factory: ToolFactory) -> None:
        """Registra uma tool disponível para todos os tenants, independente do vertical."""
        self._generic.append(factory)

    def register_vertical(self, vertical: str, factory: ToolFactory) -> None:
        """Registra uma tool disponível apenas para tenants do vertical informado."""
        self._by_vertical.setdefault(vertical, []).append(factory)

    def get_tools(self, vertical: str, tenant_id: str) -> list[BaseTool]:
        """Resolve o conjunto de tools para uma conversa: genéricas + as do vertical.

        Cada factory é chamada com tenant_id, que fica lacrado na closure da tool.
        Trocar de vertical = trocar as tools sem mexer no core.
        """
        factories = self._generic + self._by_vertical.get(vertical, [])
        tools = [f(tenant_id) for f in factories]
        return tools

    def registered_verticals(self) -> list[str]:
        """Retorna os nomes dos verticais que têm tools registradas."""
        return list(self._by_vertical.keys())


_log = logging.getLogger(__name__)

# Singleton global — instanciado uma vez no startup, compartilhado pelo app inteiro.
registry = ToolRegistry()
