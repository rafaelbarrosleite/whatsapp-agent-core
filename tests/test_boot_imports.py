"""Fumaça de boot: todo módulo de produção precisa importar sem erro.

Pega regressões como um import "não usado" removido por lint que, na verdade,
era uma reexportação usada por outro módulo — o motor não subiria.
"""

import importlib
import pathlib

import pytest

_RAIZ = pathlib.Path(__file__).parent.parent
_PACOTES = [
    "agents",
    "app",
    "channels",
    "core",
    "db",
    "gateway",
    "integrations",
    "jobs",
    "security",
    "tools",
]
_MODULOS = sorted(
    ".".join(p.relative_to(_RAIZ).with_suffix("").parts)
    for pacote in _PACOTES
    for p in (_RAIZ / pacote).rglob("*.py")
    if p.name != "__init__.py"
)


def test_encontrou_modulos() -> None:
    assert "app.main" in _MODULOS and "channels.cloud_api" in _MODULOS


@pytest.mark.parametrize("modulo", _MODULOS)
def test_modulo_importa(modulo: str) -> None:
    importlib.import_module(modulo)
