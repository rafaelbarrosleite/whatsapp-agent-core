from unittest.mock import MagicMock

from tools.verticals.clinica.leads import _make_criar_lead


def _mock_vertical(existing: list[dict] | None = None) -> tuple[MagicMock, MagicMock]:
    """Client do vertical cujo .table('leads') suporta o SELECT de existência
    (.select().eq().eq().limit().execute().data) e o .insert().execute().
    Retorna (client, tabela_mock)."""
    tabela = MagicMock()
    (
        tabela.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = existing or []
    client = MagicMock()
    client.table.return_value = tabela
    return client, tabela


def _criar(client: MagicMock, **kwargs):
    tool = _make_criar_lead(client)("tenant-xyz")
    args = {"contato_id": "c1", "nome": "Rafael Leite", "telefone": "+5567999991234"}
    args.update(kwargs)
    return tool.invoke(args)


# ─── criação ─────────────────────────────────────────────────────────────────


def test_criar_lead_insere_quando_nao_existe():
    client, tabela = _mock_vertical(existing=[])
    resultado = _criar(
        client,
        origem="indicacao_paciente",
        indicado_por="Dr. X",
        observacoes="Convênio Unimed. Nasc 10/05/1990.",
    )

    tabela.insert.assert_called_once()
    payload = tabela.insert.call_args[0][0]
    assert payload["tenant_id"] == "tenant-xyz"
    assert payload["contato_id"] == "c1"
    assert payload["nome"] == "Rafael Leite"
    assert payload["telefone"] == "+5567999991234"
    assert payload["origem"] == "indicacao_paciente"
    assert payload["indicado_por"] == "Dr. X"
    assert payload["observacoes"] == "Convênio Unimed. Nasc 10/05/1990."
    assert payload["estagio"] == "em_conversa"
    assert "sucesso" in resultado.lower()


def test_criar_lead_nao_duplica_quando_ja_existe():
    client, tabela = _mock_vertical(existing=[{"id": "lead-existente"}])
    resultado = _criar(client)

    tabela.insert.assert_not_called()
    assert "já está cadastrado" in resultado.lower() or "não criei" in resultado.lower()


def test_criar_lead_checa_por_contato_e_por_telefone():
    """A dedup consulta por contato_id E por telefone canônico (2 filtros)."""
    client, tabela = _mock_vertical(existing=[])
    _criar(client)
    segundos_eq = [c.args for c in tabela.select.return_value.eq.return_value.eq.call_args_list]
    assert ("contato_id", "c1") in segundos_eq
    assert ("telefone", "+5567999991234") in segundos_eq


def test_criar_lead_nao_duplica_quando_telefone_ja_existe_sem_contato():
    """Lead criado no CRM (sem contato_id) mas com o mesmo telefone → não duplica.
    1ª query (contato_id) vazia; 2ª (telefone) encontra."""
    client, tabela = _mock_vertical()
    chain = tabela.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute
    chain.side_effect = [MagicMock(data=[]), MagicMock(data=[{"id": "lead-crm"}])]
    resultado = _criar(client)
    tabela.insert.assert_not_called()
    assert "já está cadastrado" in resultado.lower() or "não criei" in resultado.lower()


# ─── normalizações ───────────────────────────────────────────────────────────


def test_criar_lead_origem_invalida_vira_outro():
    client, tabela = _mock_vertical(existing=[])
    _criar(client, origem="tiktok")
    assert tabela.insert.call_args[0][0]["origem"] == "outro"


def test_criar_lead_normaliza_telefone_para_e164():
    client, tabela = _mock_vertical(existing=[])
    _criar(client, telefone="556799991234")  # 12 díg, sem o 9
    assert tabela.insert.call_args[0][0]["telefone"] == "+5567999991234"


# ─── robustez / segurança ────────────────────────────────────────────────────


def test_criar_lead_erro_no_insert_nao_propaga():
    client, tabela = _mock_vertical(existing=[])
    tabela.insert.return_value.execute.side_effect = Exception("boom")
    # NÃO deve levantar; retorna mensagem amigável.
    resultado = _criar(client)
    assert "problema técnico" in resultado.lower()


def test_criar_lead_nao_expoe_tenant_id_ao_llm():
    client, _ = _mock_vertical(existing=[])
    tool = _make_criar_lead(client)("tenant-xyz")
    props = tool.args_schema.model_json_schema().get("properties", {})
    assert "tenant_id" not in props
    assert {"contato_id", "nome", "telefone"} <= set(props)
