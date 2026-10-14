from unittest.mock import MagicMock

from tools.verticals.clinica.localizacao import (
    _FALLBACK,
    _escolher_bloco,
    _make_localizacao_clinica,
)


def _mock_vertical(rows: list[dict] | None):
    tabela = MagicMock()
    (
        tabela.select.return_value.eq.return_value.like.return_value.eq.return_value.execute.return_value.data
    ) = rows if rows is not None else []
    client = MagicMock()
    client.table.return_value = tabela
    return client


def _tool(client):
    return _make_localizacao_clinica(client)("tenant-xyz")


_PRINCIPAL = {"category": "localizacao", "content": "Endereço principal"}
_SANTOS = {"category": "localizacao_santos", "content": "Santos addr"}


def test_localizacao_retorna_conteudo_do_bloco_verbatim():
    texto = "📍 Endereço: Rua X\n| Maps: https://maps.app.goo.gl/abc\n\nBem-vindo! ❤️"
    tool = _tool(_mock_vertical([{"category": "localizacao", "content": texto}]))
    assert tool.invoke({}) == texto


def test_localizacao_filtra_por_tenant_categoria_e_ativo():
    client = _mock_vertical([_PRINCIPAL])
    _tool(client).invoke({})
    tabela = client.table.return_value
    assert tabela.select.return_value.eq.call_args[0] == ("tenant_id", "tenant-xyz")
    assert tabela.select.return_value.eq.return_value.like.call_args[0] == (
        "category",
        "localizacao%",
    )
    assert tabela.select.return_value.eq.return_value.like.return_value.eq.call_args[0] == (
        "active",
        True,
    )


def test_localizacao_sem_bloco_usa_fallback():
    assert _tool(_mock_vertical([])).invoke({}) == _FALLBACK


def test_localizacao_erro_no_banco_nao_propaga_usa_fallback():
    client = MagicMock()
    client.table.side_effect = Exception("db down")
    assert _make_localizacao_clinica(client)("tenant-xyz").invoke({}) == _FALLBACK


def test_localizacao_tool_expoe_cidade_e_nao_tenant():
    tool = _tool(_mock_vertical([_PRINCIPAL]))
    props = tool.args_schema.model_json_schema().get("properties", {})
    assert "cidade" in props
    assert "tenant_id" not in props


def test_descricao_da_tool_nao_cita_clinica_especifica():
    descricao = _tool(_mock_vertical([])).description
    for nome in ("Dr. Fulano", "Santos", "Sao Paulo"):
        assert nome not in descricao


# ─── unidades (genérico: localizacao_<cidade>) ───────────────────────────────


def test_escolhe_a_unidade_da_cidade_e_cai_na_principal():
    blocos = [_PRINCIPAL, _SANTOS]
    assert _escolher_bloco(blocos, "santos") == "Santos addr"
    assert _escolher_bloco(blocos, "Santos SP") == "Santos addr"
    assert _escolher_bloco(blocos, "sao paulo") == "Endereço principal"
    assert _escolher_bloco(blocos, "") == "Endereço principal"
    assert _escolher_bloco([_SANTOS], "") is None


def test_localizacao_santos_pela_tool():
    assert _tool(_mock_vertical([_PRINCIPAL, _SANTOS])).invoke({"cidade": "santos"}) == (
        "Santos addr"
    )
