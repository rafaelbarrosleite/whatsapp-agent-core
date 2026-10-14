import logging
import math
import re
import unicodedata

from langchain_core.tools import BaseTool, tool

from core.registry import ToolFactory, ToolRegistry
from db.vertical_client import get_vertical_supabase
from supabase import Client

log = logging.getLogger(__name__)

# Quantos blocos voltam por chamada. Antes a tool devolvia a base INTEIRA (~757
# tokens) a cada chamada — e o resultado fica no histórico, sendo reenviado em
# todo turno seguinte. Devolver só os mais relevantes corta o grosso do custo
# (CLAUDE.md §5). 3 dá margem para o LLM ainda ver contexto vizinho.
_MAX_BLOCOS = 3

# Termos curtos/genéricos não discriminam bloco — só geram ruído na pontuação.
_STOPWORDS = frozenset(
    {
        "para",
        "com",
        "que",
        "por",
        "qual",
        "quais",
        "como",
        "quando",
        "onde",
        "quanto",
        "voce",
        "vcs",
        "tem",
        "temos",
        "ter",
        "fazer",
        "faz",
        "sobre",
        "meu",
        "minha",
        "mim",
        "sua",
        "seu",
        "esse",
        "essa",
        "isso",
        "aqui",
        "sao",
        "esta",
        "estao",
        "ser",
        "pode",
        "posso",
        "gostaria",
        "queria",
        "preciso",
        "dos",
        "das",
        "nao",
        "sim",
        "ola",
        "bom",
        "dia",
        "boa",
    }
)


def _normalizar(texto: str) -> str:
    """minúsculas + sem acento — 'Convênio' e 'convenio' têm que casar."""
    sem_acento = unicodedata.normalize("NFKD", texto or "")
    return sem_acento.encode("ascii", "ignore").decode("ascii").lower()


# Radical grosseiro: compara só o começo da palavra. É o que faz a flexão do
# português casar — "infiltracao" x "infiltracoes" (ão/ões) não casa por
# substring, mas o radical "infil" casa; idem "urgente" x "urgencia".
_TAMANHO_RADICAL = 5


def _radicais(texto: str) -> list[str]:
    """Radicais dos termos significativos (>= 4 letras, fora das stopwords), com
    repetição — o BM25 conta quantas vezes o termo aparece."""
    return [
        p[:_TAMANHO_RADICAL]
        for p in re.split(r"[^a-z0-9]+", _normalizar(texto))
        if len(p) >= 4 and p not in _STOPWORDS
    ]


def _termos(texto: str) -> set[str]:
    return set(_radicais(texto))


# ── BM25 (Sprint 2 do plano FIAP — rag/retriever.py do Tech Challenge) ──────
# Contagem simples trata todo termo igual: "consulta", que está em quase todo
# bloco, pesava o mesmo que "infiltração", que está em um só. O BM25 dá peso
# pelo quão raro o termo é na base (IDF) e não deixa um bloco longo ganhar só
# por repetir palavras (saturação k1 + normalização por tamanho b).
#
# Da FIAP veio só a fórmula. O stemmer Snowball de lá ficou de fora: ele leva
# "urgente" a "urgent" e "urgência" a "urgênc", que não casam — o radical de 5
# letras daqui casa (tests/test_conhecimento_tool.py::test_flexao_urgente...).
_BM25_K1 = 1.5
_BM25_B = 0.75
# Categoria e título são o rótulo do assunto: entram repetidos no documento.
_PESO_ROTULO = 3


def _documento(bloco: dict) -> list[str]:
    rotulo = _radicais(f"{bloco.get('category') or ''} {bloco.get('title') or ''}")
    return rotulo * _PESO_ROTULO + _radicais(bloco.get("content") or "")


def _pontuar_bm25(blocos: list[dict], termos: set[str]) -> list[float]:
    """Score BM25 de cada bloco para os termos da pergunta (0 = sem casamento)."""
    documentos = [_documento(b) for b in blocos]
    n = len(documentos)
    media = sum(len(d) for d in documentos) / n if n else 0.0
    presentes = [set(d) for d in documentos]
    idf = {}
    for termo in termos:
        com_termo = sum(1 for p in presentes if termo in p)
        idf[termo] = math.log((n - com_termo + 0.5) / (com_termo + 0.5) + 1)

    scores = []
    for documento in documentos:
        tamanho = len(documento) / media if media else 0.0
        score = 0.0
        for termo in termos:
            f = documento.count(termo)
            if f:
                score += (
                    idf[termo]
                    * f
                    * (_BM25_K1 + 1)
                    / (f + _BM25_K1 * (1 - _BM25_B + _BM25_B * tamanho))
                )
        scores.append(score)
    return scores


def _make_buscar_conhecimento(vertical_client: Client) -> ToolFactory:
    """Retorna uma factory que cria a tool buscar_conhecimento com o cliente injetado."""

    def factory(tenant_id: str) -> BaseTool:
        # tenant_id fica lacrado nesta closure — o LLM só vê `query`.
        @tool
        def buscar_conhecimento(query: str) -> str:
            """Busca informações sobre a clínica: tratamentos, convênios,
            horários, endereço, procedimentos e dúvidas frequentes.
            Use quando o paciente perguntar sobre qualquer informação
            específica da clínica."""
            resultado = (
                vertical_client.table("agent_knowledge_blocks")
                .select("title, content, category")
                .eq("tenant_id", tenant_id)  # filtro forçado — nunca omitir
                .eq("active", True)
                .order("priority", desc=True)
                .execute()
            )

            if not resultado.data:
                return "Informação não encontrada. Oriente o paciente a contatar a equipe."

            # Seleciona só os blocos relevantes à query. O SELECT continua
            # trazendo tudo (é barato no banco); o que custa caro é o texto que
            # volta ao LLM e depois fica no histórico — por isso o corte é aqui.
            todos = resultado.data
            termos = _termos(query)
            pontuados = sorted(
                (
                    (pontos, i, b)
                    for i, (pontos, b) in enumerate(zip(_pontuar_bm25(todos, termos), todos))
                ),
                key=lambda x: (-x[0], x[1]),  # score desc; empate mantém priority
            )
            escolhidos = [b for pontos, _, b in pontuados if pontos > 0][:_MAX_BLOCOS]

            # Sem nenhum casamento (query vaga, termo fora da base) devolve os
            # de maior prioridade em vez de nada — melhor um contexto genérico
            # que uma resposta "não encontrei" para algo que a base cobre.
            if not escolhidos:
                escolhidos = todos[:_MAX_BLOCOS]
                log.info("buscar_conhecimento: sem match — usando top prioridade")

            log.info("buscar_conhecimento: %d de %d blocos", len(escolhidos), len(todos))
            blocos = [
                f"[{(bloco.get('category') or '').upper()}] {bloco['title']}:\n{bloco['content']}"
                for bloco in escolhidos
            ]
            return "\n\n".join(blocos)

        return buscar_conhecimento

    return factory


def registrar(registry: ToolRegistry, vertical_client: Client | None = None) -> None:
    """Registra as tools de conhecimento como exclusivas do vertical clínica.

    Aceita um client externo para facilitar testes sem conexão real ao Supabase.
    """
    _client = vertical_client or get_vertical_supabase()
    registry.register_vertical("clinica", _make_buscar_conhecimento(_client))
