from unittest.mock import MagicMock

from tools.crm.contatos import _make_buscar_contato, _make_criar_contato

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_select(rows: list[dict]) -> MagicMock:
    """Mock do chain client.table().select().eq().eq().execute()"""
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = rows
    return client


def _mock_insert(row: dict) -> MagicMock:
    """Mock do chain client.table().insert().execute()"""
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.return_value.data = [row]
    return client


# ---------------------------------------------------------------------------
# buscar_contato
# ---------------------------------------------------------------------------


def test_buscar_contato_retorna_contato_encontrado():
    contato = {"id": "uuid-1", "nome": "João", "telefone": "+5511999990000"}
    client = _mock_select([contato])

    buscar = _make_buscar_contato(client)("tenant-xyz")
    result = buscar.invoke({"telefone": "+5511999990000"})

    assert result == contato


def test_buscar_contato_retorna_none_quando_nao_encontrado():
    client = _mock_select([])

    buscar = _make_buscar_contato(client)("tenant-xyz")
    result = buscar.invoke({"telefone": "+5511000000000"})

    assert result is None


def test_buscar_contato_normaliza_telefone_do_llm():
    """Regressão: o LLM pode passar o número em formato diferente do gravado
    (sem DDI, sem 9, com máscara). A busca deve normalizar antes do .eq() para
    casar com a forma canônica — senão erra o match e o agente cria duplicata."""
    client = _mock_select([])

    buscar = _make_buscar_contato(client)("tenant-xyz")
    buscar.invoke({"telefone": "556799991234"})  # 12 díg, sem o 9

    # O segundo .eq() (telefone) fica encadeado no return do primeiro .eq().
    segundo_eq = client.table.return_value.select.return_value.eq.return_value.eq
    filtro_telefone = segundo_eq.call_args[0]
    assert filtro_telefone == ("telefone", "+5567999991234")


def test_criar_contato_normaliza_telefone_do_llm():
    """Regressão: criação deve gravar sempre na forma canônica E.164, inserindo
    o 9 omitido quando o número vem com DDI + DDD + 8 dígitos."""
    client = _mock_insert({"id": "uuid-x", "telefone": "+5567999991234", "tenant_id": "t1"})

    criar = _make_criar_contato(client)("t1")
    criar.invoke({"telefone": "556799991234@s.whatsapp.net"})  # 12 díg + sufixo

    payload = client.table.return_value.insert.call_args[0][0]
    assert payload["telefone"] == "+5567999991234"


def test_buscar_contato_filtra_por_tenant_id():
    """A primeira chamada a .eq() deve ser com tenant_id — nunca telefone antes do tenant."""
    client = _mock_select([])

    buscar = _make_buscar_contato(client)("tenant-abc")
    buscar.invoke({"telefone": "+5511999990000"})

    eq_calls = client.table.return_value.select.return_value.eq.call_args_list
    primeiro_filtro = eq_calls[0][0]  # (coluna, valor) do primeiro .eq()
    assert primeiro_filtro == ("tenant_id", "tenant-abc")


def test_buscar_contato_nao_expoe_tenant_id_ao_llm():
    """tenant_id não pode aparecer no schema que o LLM recebe."""
    client = _mock_select([])
    buscar = _make_buscar_contato(client)("tenant-abc")

    schema = buscar.args_schema.model_json_schema()
    assert "tenant_id" not in schema.get("properties", {})


# ---------------------------------------------------------------------------
# criar_contato
# ---------------------------------------------------------------------------


def test_criar_contato_retorna_contato_criado():
    novo = {"id": "uuid-2", "nome": "Maria", "telefone": "+5511888880000", "tenant_id": "t1"}
    client = _mock_insert(novo)

    criar = _make_criar_contato(client)("t1")
    result = criar.invoke({"telefone": "+5511888880000", "nome": "Maria"})

    assert result == novo


def test_criar_contato_inclui_tenant_id_no_payload():
    """tenant_id deve estar no dict enviado ao banco — nunca omitir."""
    client = _mock_insert({"id": "uuid-3", "telefone": "+5511777770000", "tenant_id": "t1"})

    criar = _make_criar_contato(client)("t1")
    criar.invoke({"telefone": "+5511777770000"})

    payload = client.table.return_value.insert.call_args[0][0]
    assert payload["tenant_id"] == "t1"
    assert payload["telefone"] == "+5511777770000"


def test_criar_contato_nao_expoe_tenant_id_ao_llm():
    """tenant_id não pode aparecer no schema que o LLM recebe."""
    client = _mock_insert({})
    criar = _make_criar_contato(client)("t1")

    schema = criar.args_schema.model_json_schema()
    assert "tenant_id" not in schema.get("properties", {})
