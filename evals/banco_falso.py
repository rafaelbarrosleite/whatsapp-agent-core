"""evals/banco_falso.py — Supabase em memória para a avaliação do agente.

A avaliação mede o comportamento do LLM (que tool chama, se inventa número,
se resiste a prompt injection), não o banco. Por isso as tools rodam de verdade
contra este cliente falso, que entende o encadeamento usado no código
(`table().select().eq().order().limit().execute()`, `insert`, `update`...).

Filtros que ele não conhece (ex.: `like`) são ignorados: devolvem todas as
linhas da tabela, o que basta para os casos de avaliação.
"""

import copy
import uuid
from types import SimpleNamespace
from typing import Any


class _Consulta:
    def __init__(self, banco: "BancoFalso", tabela: str) -> None:
        self._banco = banco
        self._tabela = tabela
        self._filtros: list = []
        self._ordem: tuple[str, bool] | None = None
        self._limite: int | None = None
        self._acao = "select"
        self._valores: Any = None
        self._uma = False

    # ── filtros ────────────────────────────────────────────────────────────
    def _filtro(self, coluna: str, teste) -> "_Consulta":
        self._filtros.append(lambda linha: coluna not in linha or teste(linha[coluna]))
        return self

    def eq(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v == valor)

    def neq(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v != valor)

    def in_(self, coluna: str, valores: list) -> "_Consulta":
        return self._filtro(coluna, lambda v: v in valores)

    def gte(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v is not None and str(v) >= str(valor))

    def gt(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v is not None and str(v) > str(valor))

    def lte(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v is not None and str(v) <= str(valor))

    def lt(self, coluna: str, valor: Any) -> "_Consulta":
        return self._filtro(coluna, lambda v: v is not None and str(v) < str(valor))

    def is_(self, coluna: str, valor: Any) -> "_Consulta":
        alvo = None if valor in (None, "null") else valor
        return self._filtro(coluna, lambda v: v is alvo or v == alvo)

    def order(self, coluna: str, desc: bool = False, **_: Any) -> "_Consulta":
        self._ordem = (coluna, desc)
        return self

    def limit(self, n: int) -> "_Consulta":
        self._limite = n
        return self

    def maybe_single(self) -> "_Consulta":
        self._uma = True
        return self

    single = maybe_single

    def __getattr__(self, _nome: str):
        # select, like, ilike, range, not_... — aceitos e ignorados.
        return lambda *a, **k: self

    # ── escrita ────────────────────────────────────────────────────────────
    def insert(self, valores: Any, **_: Any) -> "_Consulta":
        self._acao, self._valores = "insert", valores
        return self

    upsert = insert

    def update(self, valores: dict) -> "_Consulta":
        self._acao, self._valores = "update", valores
        return self

    def delete(self) -> "_Consulta":
        self._acao = "delete"
        return self

    # ── execução ───────────────────────────────────────────────────────────
    def _selecionadas(self) -> list[dict]:
        linhas = self._banco.tabelas.setdefault(self._tabela, [])
        return [linha for linha in linhas if all(f(linha) for f in self._filtros)]

    def execute(self) -> SimpleNamespace:
        linhas = self._banco.tabelas.setdefault(self._tabela, [])
        if self._acao == "insert":
            novas = self._valores if isinstance(self._valores, list) else [self._valores]
            novas = [{"id": str(uuid.uuid4()), **copy.deepcopy(n)} for n in novas]
            linhas.extend(novas)
            self._banco.escritas.append((self._tabela, "insert", novas))
            return SimpleNamespace(data=novas)
        if self._acao == "update":
            alvo = self._selecionadas()
            for linha in alvo:
                linha.update(self._valores)
            self._banco.escritas.append((self._tabela, "update", alvo))
            return SimpleNamespace(data=alvo)
        if self._acao == "delete":
            alvo = self._selecionadas()
            self._banco.tabelas[self._tabela] = [x for x in linhas if x not in alvo]
            return SimpleNamespace(data=alvo)

        resultado = copy.deepcopy(self._selecionadas())
        if self._ordem:
            coluna, desc = self._ordem
            resultado.sort(key=lambda x: (x.get(coluna) is None, x.get(coluna)), reverse=desc)
        if self._limite is not None:
            resultado = resultado[: self._limite]
        if self._uma:
            return SimpleNamespace(data=resultado[0] if resultado else None)
        return SimpleNamespace(data=resultado)


class BancoFalso:
    """Cliente no formato do supabase-py, com as tabelas num dict."""

    def __init__(self, tabelas: dict[str, list[dict]] | None = None) -> None:
        self.tabelas: dict[str, list[dict]] = copy.deepcopy(tabelas or {})
        self.escritas: list[tuple[str, str, list[dict]]] = []

    def table(self, nome: str) -> _Consulta:
        return _Consulta(self, nome)

    def rpc(self, *_: Any, **__: Any) -> SimpleNamespace:
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))
