"""tools/verticals/clinica/localizacao.py — tool localizacao_clinica.

Devolve o texto OFICIAL de localização/endereço da clínica, lido VERBATIM do
knowledge base do tenant (bloco category='localizacao'). Assim o texto é
tenant-específico (fica no banco, não hardcode no core) e determinístico: o agente
envia sempre o mesmo texto, com o link do Maps que gera o preview do local no
WhatsApp (preview_url habilitado no CloudApiSender).
"""

import logging
import re
import unicodedata

from langchain_core.tools import BaseTool, tool

from core.registry import ToolFactory, ToolRegistry
from db.vertical_client import get_vertical_supabase
from supabase import Client

log = logging.getLogger(__name__)

_FALLBACK = (
    "Não consegui carregar o endereço agora. Por favor, peça a localização à "
    "nossa recepção que te ajudamos! 🙏"
)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFD", (texto or "").lower())
    sem_acento = "".join(c for c in sem_acento if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", sem_acento).strip("_")


def _escolher_bloco(blocos: list[dict], cidade: str) -> str | None:
    """Unidade da cidade pedida ('localizacao_<cidade>'), senão a principal ('localizacao').

    Genérico para qualquer clínica: cada unidade extra é um bloco de
    conhecimento com category 'localizacao_<cidade>' (ex.: localizacao_santos);
    a principal é 'localizacao'.
    """
    pedido = _normalizar(cidade)
    principal: str | None = None
    for bloco in blocos:
        categoria = bloco.get("category") or ""
        conteudo = bloco.get("content")
        if not conteudo:
            continue
        if categoria == "localizacao":
            principal = conteudo
            continue
        sufixo = categoria.removeprefix("localizacao_")
        if pedido and sufixo and sufixo in pedido:
            return conteudo
    return principal


def _make_localizacao_clinica(vertical_client: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        # tenant_id lacrado na closure — o LLM só informa a cidade.
        @tool
        def localizacao_clinica(cidade: str = "") -> str:
            """Retorna o endereço/localização OFICIAL da clínica. Use SEMPRE que o
            paciente perguntar endereço, onde fica ou como chegar. Se a clínica
            tiver mais de uma unidade (o seu prompt diz quais), pergunte a cidade
            e passe em `cidade`; senão, deixe vazio. Envie ao paciente EXATAMENTE
            o texto retornado, sem reformular (o link do Maps gera o preview)."""
            try:
                r = (
                    vertical_client.table("agent_knowledge_blocks")
                    .select("category, content")
                    .eq("tenant_id", tenant_id)  # filtro forçado — nunca omitir
                    .like("category", "localizacao%")
                    .eq("active", True)
                    .execute()
                )
            except Exception:
                log.error(
                    "localizacao_clinica: erro ao ler blocos | tenant=%s", tenant_id, exc_info=True
                )
                return _FALLBACK
            texto = _escolher_bloco(r.data or [], cidade)
            if texto:
                return texto
            log.warning(
                "localizacao_clinica: sem bloco de localização ativo | tenant=%s", tenant_id
            )
            return _FALLBACK

        return localizacao_clinica

    return factory


def registrar(registry: ToolRegistry, vertical_client: Client | None = None) -> None:
    """Registra a tool localizacao_clinica como exclusiva do vertical clínica."""
    _client = vertical_client or get_vertical_supabase()
    registry.register_vertical("clinica", _make_localizacao_clinica(_client))
