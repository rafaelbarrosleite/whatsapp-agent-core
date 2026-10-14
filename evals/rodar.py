"""evals/rodar.py — avaliação do agente com o Claude de verdade.

Roda cada caso de evals/casos.toml pelo grafo real (guardrail de entrada →
agente com tools → guardrail de saída), com um banco em memória no lugar do
Supabase, e confere a resposta com regras objetivas. Sai com código 1 se a taxa
de acerto ficar abaixo do mínimo — é o que o CI usa para barrar uma mudança de
prompt ou de tool que piore o agente.

Uso:
    ANTHROPIC_API_KEY=... uv run python -m evals.rodar
    uv run python -m evals.rodar --casos preco_da_consulta --repeticoes 3

Por que regras e não um LLM juiz: as perguntas que importam aqui têm resposta
objetiva (chamou a tool certa? inventou preço? passou o código interno?). Regra
é barata, determinística e não precisa de calibração. Um juiz por LLM entra
quando houver critério subjetivo (tom, clareza).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import tomllib
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from evals.banco_falso import BancoFalso

RAIZ = Path(__file__).resolve().parent
CASOS = RAIZ / "casos.toml"
TENANT_ID = "t-eval"


@dataclass
class Resultado:
    id: str
    passou: bool
    falhas: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resposta: str = ""
    segundos: float = 0.0


# ── Montagem do ambiente ─────────────────────────────────────────────────────


def montar_banco(clinica: dict) -> BancoFalso:
    return BancoFalso(
        {
            "tenants": [
                {
                    "id": TENANT_ID,
                    "nome": clinica["nome"],
                    "vertical": "clinica",
                    "system_prompt": clinica["system_prompt"],
                    "config": clinica.get("config", {}),
                }
            ],
            "contatos": [
                {
                    "id": clinica["contato_id"],
                    "tenant_id": TENANT_ID,
                    "telefone": clinica["telefone"],
                }
            ],
            "agent_knowledge_blocks": [
                {"tenant_id": TENANT_ID, "active": True, **bloco}
                for bloco in clinica.get("conhecimento", [])
            ],
            "agendamentos": [],
        }
    )


class BancoAtual:
    """Um cliente só para o processo inteiro, apontando para o banco do caso.

    As tools guardam o cliente no momento do registro (closure). Se cada caso
    registrasse um banco novo, um caso enxergaria o que o anterior gravou;
    com este proxy, trocar `atual` antes de cada caso basta.
    """

    def __init__(self) -> None:
        self.atual = BancoFalso()

    def table(self, nome: str):
        return self.atual.table(nome)

    def rpc(self, *args, **kwargs):
        return self.atual.rpc(*args, **kwargs)


def _modulos_do_projeto() -> list:
    pacotes = (
        "app.",
        "core.",
        "db.",
        "tools.",
        "gateway.",
        "channels.",
        "integrations.",
        "security.",
    )
    return [m for nome, m in list(sys.modules.items()) if nome.startswith(pacotes) and m]


def banco_no_lugar_do_supabase(banco) -> ExitStack:
    """Troca get_supabase/get_vertical_supabase em todo módulo que os importou.

    Importa antes o grafo e as tools, para que todos os módulos já estejam
    carregados quando a troca acontece."""
    import app.tools_setup  # noqa: F401
    import core.graph  # noqa: F401
    import core.nodes  # noqa: F401
    import tools.verticals.clinica.agenda_demo  # noqa: F401

    pilha = ExitStack()
    for modulo in _modulos_do_projeto():
        for nome in ("get_supabase", "get_vertical_supabase"):
            if hasattr(modulo, nome):
                pilha.enter_context(patch.object(modulo, nome, lambda: banco))
    return pilha


# ── Conferência ──────────────────────────────────────────────────────────────


def _texto(conteudo) -> str:
    if isinstance(conteudo, str):
        return conteudo
    return " ".join(b.get("text", "") for b in conteudo if isinstance(b, dict))


def conferir(caso: dict, mensagens: list, referencias: list[str]) -> Resultado:
    """Aplica as regras do caso às mensagens novas do turno."""
    from security.aderencia import numeros_sem_fonte

    chamadas = [tc for m in mensagens if isinstance(m, AIMessage) for tc in m.tool_calls]
    nomes = [tc["name"] for tc in chamadas]
    finais = [m for m in mensagens if isinstance(m, AIMessage) and _texto(m.content).strip()]
    resposta = _texto(finais[-1].content) if finais else ""
    falhas: list[str] = []

    for tool in caso.get("tools_esperadas", []):
        if tool not in nomes:
            falhas.append(f"não chamou {tool}")
    alguma = caso.get("tools_alguma", [])
    if alguma and not set(alguma) & set(nomes):
        falhas.append(f"não chamou nenhuma de {alguma}")
    for tool in caso.get("tools_proibidas", []):
        if tool in nomes:
            falhas.append(f"chamou {tool}, que não devia")
    for tool, esperados in caso.get("args_esperados", {}).items():
        for chamada in (c for c in chamadas if c["name"] == tool):
            for arg, valor in esperados.items():
                recebido = chamada["args"].get(arg)
                if recebido != valor:
                    falhas.append(f"{tool}.{arg}={recebido!r}, esperado {valor!r}")
    for padrao in caso.get("contem", []):
        if not re.search(padrao, resposta, re.IGNORECASE):
            falhas.append(f"resposta sem /{padrao}/")
    for padrao in caso.get("nao_contem", []):
        if re.search(padrao, resposta, re.IGNORECASE):
            falhas.append(f"resposta com /{padrao}/")
    if caso.get("numeros_com_fonte"):
        sem_fonte = numeros_sem_fonte(resposta, referencias)
        if sem_fonte:
            falhas.append(f"números sem fonte: {sem_fonte}")
    if not resposta:
        falhas.append("sem resposta final")

    return Resultado(
        id=caso["id"], passou=not falhas, falhas=falhas, tools=nomes, resposta=resposta
    )


# ── Execução ─────────────────────────────────────────────────────────────────


async def rodar_caso(caso: dict, clinica: dict, banco: BancoAtual) -> Resultado:
    """Roda um caso. Espera o banco já no lugar do Supabase e as tools registradas."""
    from core.graph import RECURSION_LIMIT, criar_grafo
    from core.nodes import _referencias

    banco.atual = montar_banco(clinica)
    historico = [
        HumanMessage(content=texto) if papel == "paciente" else AIMessage(content=texto)
        for papel, texto in caso.get("historico", [])
    ]
    entrada = {
        "messages": [*historico, HumanMessage(content=caso["mensagem"])],
        "tenant_id": TENANT_ID,
        "telefone": clinica["telefone"],
        "contato_id": None,
    }
    config = {"configurable": {"thread_id": caso["id"]}, "recursion_limit": RECURSION_LIMIT}

    inicio = time.monotonic()
    estado = await criar_grafo().ainvoke(entrada, config=config)
    novas = estado["messages"][len(historico) :]
    resultado = conferir(caso, novas, _referencias(estado))
    resultado.segundos = round(time.monotonic() - inicio, 1)
    return resultado


def preparar_registry() -> None:
    """Registra as mesmas tools do servidor (uma vez por processo)."""
    from app.tools_setup import registrar_tools
    from core.registry import registry

    if not registry.registered_verticals():
        registrar_tools(registry)


def carregar(caminho: Path = CASOS) -> tuple[dict, list[dict]]:
    dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    return dados["clinica"], dados["casos"]


def relatorio(resultados: list[Resultado], minimo: float) -> str:
    acertos = sum(r.passou for r in resultados)
    taxa = acertos / len(resultados) if resultados else 0.0
    linhas = [
        f"## Avaliação do agente: {acertos}/{len(resultados)} ({taxa:.0%}, mínimo {minimo:.0%})",
        "",
        "| caso | resultado | tools chamadas | falhas |",
        "|---|---|---|---|",
    ]
    for r in resultados:
        linhas.append(
            f"| {r.id} | {'✅' if r.passou else '❌'} | {', '.join(r.tools) or '—'} "
            f"| {'; '.join(r.falhas) or ''} |"
        )
    return "\n".join(linhas)


async def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--casos", nargs="*", help="ids dos casos (padrão: todos)")
    parser.add_argument("--repeticoes", type=int, default=1, help="rodadas por caso")
    parser.add_argument("--minimo", type=float, default=0.9, help="taxa mínima de acerto")
    parser.add_argument("--saida", type=Path, default=RAIZ / "resultado.json")
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY não definida: a avaliação chama o Claude de verdade.")
        return 2

    clinica, casos = carregar()
    if args.casos:
        casos = [c for c in casos if c["id"] in set(args.casos)]
    banco = BancoAtual()
    resultados: list[Resultado] = []
    with banco_no_lugar_do_supabase(banco):
        preparar_registry()
        for caso in casos:
            for _ in range(args.repeticoes):
                try:
                    resultado = await rodar_caso(caso, clinica, banco)
                except Exception as exc:  # um caso quebrado não derruba a bateria
                    resultado = Resultado(id=caso["id"], passou=False, falhas=[f"erro: {exc!r}"])
                print(
                    f"{'✅' if resultado.passou else '❌'} {resultado.id} {resultado.falhas or ''}"
                )
                if not resultado.passou:  # a resposta no log basta para diagnosticar
                    print("   resposta: " + resultado.resposta.replace("\n", " ")[:500])
                resultados.append(resultado)

    texto = relatorio(resultados, args.minimo)
    print("\n" + texto)
    if resumo := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(resumo, "a", encoding="utf-8") as arquivo:
            arquivo.write(texto + "\n")
    args.saida.write_text(
        json.dumps([asdict(r) for r in resultados], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    taxa = sum(r.passou for r in resultados) / len(resultados) if resultados else 0.0
    return 0 if taxa >= args.minimo else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
