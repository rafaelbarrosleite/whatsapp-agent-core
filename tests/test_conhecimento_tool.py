from unittest.mock import MagicMock

from tools.verticals.clinica.conhecimento import _MAX_BLOCOS, _make_buscar_conhecimento

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_select(rows: list[dict]) -> MagicMock:
    """Mock do chain client.table().select().eq().eq().order().execute()"""
    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.order.return_value.execute.return_value.data = rows
    return client


# ---------------------------------------------------------------------------
# buscar_conhecimento
# ---------------------------------------------------------------------------


def test_busca_retorna_blocos_do_tenant():
    blocos = [
        {
            "title": "Localização e Contato",
            "content": "Rua das Flores, 100.",
            "category": "localizacao",
        },
        {"title": "Como Agendar Consulta", "content": "Pelo WhatsApp.", "category": "agendamento"},
    ]
    client = _mock_select(blocos)

    buscar = _make_buscar_conhecimento(client)("tenant-xyz")
    result = buscar.invoke({"query": "endereço da clínica"})

    assert "[LOCALIZACAO] Localização e Contato:\nRua das Flores, 100." in result
    assert "[AGENDAMENTO] Como Agendar Consulta:\nPelo WhatsApp." in result


def test_tenant_errado_retorna_vazio():
    """Tenant sem blocos cadastrados (ou tenant_id incorreto) recebe mensagem de fallback."""
    client = _mock_select([])

    buscar = _make_buscar_conhecimento(client)("tenant-sem-blocos")
    result = buscar.invoke({"query": "horário de atendimento"})

    assert result == "Informação não encontrada. Oriente o paciente a contatar a equipe."

    eq_calls = client.table.return_value.select.return_value.eq.call_args_list
    primeiro_filtro = eq_calls[0][0]  # (coluna, valor) do primeiro .eq()
    assert primeiro_filtro == ("tenant_id", "tenant-sem-blocos")


def test_tool_nao_expoe_tenant_id_ao_llm():
    """tenant_id não pode aparecer no schema que o LLM recebe."""
    client = _mock_select([])
    buscar = _make_buscar_conhecimento(client)("tenant-abc")

    schema = buscar.args_schema.model_json_schema()
    assert "tenant_id" not in schema.get("properties", {})
    assert set(schema.get("properties", {})) == {"query"}


# ---------------------------------------------------------------------------
# Relevância — devolver a base inteira a cada chamada custava ~757 tokens, e o
# resultado ainda ficava no histórico sendo reenviado a cada turno (CLAUDE.md §5)
# ---------------------------------------------------------------------------

_BASE = [
    {
        "title": "Planos de Saúde",
        "content": "Aceitamos Unimed e Bradesco Saúde.",
        "category": "plano de saude",
    },
    {
        "title": "Como Agendar Consulta",
        "content": "Agende pelo WhatsApp.",
        "category": "agendamento",
    },
    {
        "title": "Infiltrações e Procedimentos",
        "content": "Fazemos infiltração no joelho.",
        "category": "tratamentos",
    },
    {
        "title": "Situações de Urgência",
        "content": "Em caso de urgência, ligue.",
        "category": "urgencia",
    },
    {"title": "Sobre a Dra. Ana", "content": "Ortopedista de joelho.", "category": "sobre"},
]


def test_traz_so_o_bloco_relevante_e_nao_a_base_inteira():
    buscar = _make_buscar_conhecimento(_mock_select(_BASE))("t-1")
    resultado = buscar.invoke({"query": "quais convênios vocês aceitam?"})

    assert "Unimed e Bradesco Saúde" in resultado
    assert "Ortopedista de joelho" not in resultado  # bloco irrelevante ficou fora
    assert len(resultado) < sum(len(b["content"]) for b in _BASE)


def test_respeita_teto_de_blocos():
    """Query que casa com tudo ainda assim é limitada — é o teto que garante
    que o resultado não volte a inchar quando a base crescer."""
    base = [
        {"title": f"Joelho {i}", "content": "joelho joelho joelho", "category": "tratamentos"}
        for i in range(10)
    ]
    buscar = _make_buscar_conhecimento(_mock_select(base))("t-1")
    resultado = buscar.invoke({"query": "joelho"})

    assert resultado.count("[TRATAMENTOS]") == _MAX_BLOCOS


def test_plural_e_acento_casam():
    """Regressão: 'infiltração' não é substring de 'Infiltrações' (ão/ões) —
    sem radical, a pergunta mais óbvia sobre o procedimento não achava o bloco."""
    buscar = _make_buscar_conhecimento(_mock_select(_BASE))("t-1")

    for pergunta in ("vocês fazem infiltração?", "faz infiltracao", "quero infiltrações"):
        assert "Fazemos infiltração no joelho" in buscar.invoke({"query": pergunta}), pergunta


def test_flexao_urgente_casa_com_urgencia():
    buscar = _make_buscar_conhecimento(_mock_select(_BASE))("t-1")
    assert "Em caso de urgência" in buscar.invoke({"query": "é urgente!"})


def test_sem_match_devolve_prioritarios_em_vez_de_nada():
    """Saudação/termo fora da base não pode devolver vazio — a ordem do SELECT
    é por priority, então o topo é o fallback sensato."""
    buscar = _make_buscar_conhecimento(_mock_select(_BASE))("t-1")
    resultado = buscar.invoke({"query": "oi bom dia"})

    assert resultado.strip()
    assert "Aceitamos Unimed" in resultado  # 1º por prioridade
    assert resultado.count("[") == _MAX_BLOCOS


def test_categoria_pesa_mais_que_conteudo():
    """O termo no rótulo (categoria/título) tem que rankear acima de uma menção
    solta no corpo de outro bloco."""
    base = [
        {"title": "Outro assunto", "content": "cita agendamento de passagem.", "category": "faq"},
        {"title": "Como Agendar", "content": "Detalhes.", "category": "agendamento"},
    ]
    buscar = _make_buscar_conhecimento(_mock_select(base))("t-1")
    resultado = buscar.invoke({"query": "agendamento"})

    assert resultado.index("[AGENDAMENTO]") < resultado.index("[FAQ]")


def test_termo_raro_pesa_mais_que_termo_comum():
    """BM25 (Sprint 2 do plano FIAP): 'consulta' aparece em quase todo bloco e
    não discrimina; 'infiltração' aparece em um só e decide. Na contagem simples
    os dois blocos empatavam e ganhava o de maior prioridade (o errado)."""
    base = [
        {
            "title": "Valor da consulta",
            "content": "A consulta custa R$ 400.",
            "category": "valores",
        },
        {"title": "Consulta por convênio", "content": "Aceitamos Unimed.", "category": "convenio"},
        {"title": "Retorno de consulta", "content": "Retorno em 30 dias.", "category": "retorno"},
        {
            "title": "Infiltração",
            "content": "A infiltração custa R$ 800.",
            "category": "procedimentos",
        },
    ]
    buscar = _make_buscar_conhecimento(_mock_select(base))("t-1")
    resultado = buscar.invoke({"query": "quanto custa a infiltração depois da consulta?"})

    assert resultado.startswith("[PROCEDIMENTOS] Infiltração")
