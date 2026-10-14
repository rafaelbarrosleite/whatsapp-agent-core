"""Testes de security/token_compartilhado.py — comparação de segredos."""

from unittest.mock import patch

import pytest

from security.token_compartilhado import token_valido


def test_comparacao_usa_tempo_constante() -> None:
    """A validação delega a hmac.compare_digest (não usa ==)."""
    with patch("security.token_compartilhado.hmac.compare_digest", return_value=True) as compare:
        assert token_valido("a", "b") is True
    compare.assert_called_once_with(b"a", b"b")


@pytest.mark.parametrize(
    ("recebido", "esperado", "resultado"),
    [
        (None, "x", False),
        ("", "x", False),
        ("x", None, False),
        ("x", "", False),
        ("x", "x", True),
        ("x", "y", False),
        ("segredo12", "segredo123", False),
    ],
)
def test_token_valido_casos(recebido, esperado, resultado) -> None:
    assert token_valido(recebido, esperado) is resultado
