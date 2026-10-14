from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from core.nodes import (
    AGENDA_TOOLS,
    HANDOFF_TOOLS,
    _tools_disponiveis,
    agente,
    carregar_tenant,
    deve_usar_tools,
    executar_tools,
    identificar_contato,
)


def _texto_system(mock_llm) -> str:
    """System prompt completo como texto. Desde o prompt caching, o system vai
    como lista de blocos ([0] estável/cacheado, [1] volátil) — ver core.nodes.agente."""
    conteudo = mock_llm.bind_tools.return_value.invoke.call_args[0][0][0].content
    if isinstance(conteudo, str):
        return conteudo
    return "".join(bloco["text"] for bloco in conteudo)


# ---------------------------------------------------------------------------
# Fixture: estado base
# ---------------------------------------------------------------------------


def estado(**overrides) -> dict:
    base = {
        "messages": [HumanMessage(content="Olá")],
        "tenant_id": "tenant-123",
        "telefone": "+5511999990000",
        "contato_id": None,
        "vertical": "",
        "system_prompt": "",
        "tenant_config": {},
        "intencao": None,
    }
    base.update(overrides)
    return base


def _mock_tool(nome: str) -> MagicMock:
    t = MagicMock()
    t.name = nome
    return t


# ---------------------------------------------------------------------------
# carregar_tenant
# ---------------------------------------------------------------------------


def _mock_supabase_tenant(
    vertical: str, system_prompt: str, config: dict | None = None
) -> MagicMock:
    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = [
        {"vertical": vertical, "system_prompt": system_prompt, "config": config or {}}
    ]
    return client


def test_carregar_tenant_preenche_tenant_config():
    mock_client = _mock_supabase_tenant(
        "clinica", "prompt", config={"handoff": {"habilitado": True}, "nome_agente": "Sofia"}
    )
    with patch("core.nodes.get_supabase", return_value=mock_client):
        resultado = carregar_tenant(estado(vertical=""))

    assert resultado["tenant_config"] == {"handoff": {"habilitado": True}, "nome_agente": "Sofia"}


def test_carregar_tenant_usa_dict_vazio_quando_config_e_none():
    mock_client = _mock_supabase_tenant("clinica", "prompt", config=None)
    with patch("core.nodes.get_supabase", return_value=mock_client):
        resultado = carregar_tenant(estado(vertical=""))

    assert resultado["tenant_config"] == {}


def test_carregar_tenant_preenche_vertical_e_system_prompt():
    mock_client = _mock_supabase_tenant("clinica", "Você é um assistente médico.")
    with patch("core.nodes.get_supabase", return_value=mock_client):
        resultado = carregar_tenant(estado(vertical=""))

    assert resultado["vertical"] == "clinica"
    assert resultado["system_prompt"] == "Você é um assistente médico."


def test_carregar_tenant_reconsulta_mesmo_com_vertical_ja_preenchido():
    """Regressão: tenant_config e system_prompt são mutáveis em runtime (toggle
    de feature flag na CRM) mas o checkpoint do LangGraph persiste o State entre
    turns da mesma conversa indefinidamente. Pular a reconsulta quando vertical
    já está preenchido (comportamento antigo) significa que uma conversa em
    andamento nunca vê uma mudança de config feita pelo Rafael depois do
    primeiro turno — causa raiz de handoff/agenda não pegarem o toggle."""
    mock_client = _mock_supabase_tenant(
        "clinica", "prompt novo", config={"handoff": {"habilitado": True}}
    )
    with patch("core.nodes.get_supabase", return_value=mock_client):
        resultado = carregar_tenant(estado(vertical="clinica", system_prompt="prompt velho"))

    mock_client.table.assert_called_with("tenants")
    assert resultado["tenant_config"] == {"handoff": {"habilitado": True}}
    assert resultado["system_prompt"] == "prompt novo"


def test_carregar_tenant_levanta_erro_se_tenant_nao_existe():
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    with patch("core.nodes.get_supabase", return_value=client):
        with pytest.raises(ValueError, match="Tenant não encontrado"):
            carregar_tenant(estado(vertical=""))


def test_carregar_tenant_usa_string_vazia_quando_system_prompt_e_none():
    mock_client = _mock_supabase_tenant("farmacia", None)
    with patch("core.nodes.get_supabase", return_value=mock_client):
        resultado = carregar_tenant(estado(vertical=""))

    assert resultado["system_prompt"] == ""


# ---------------------------------------------------------------------------
# identificar_contato
# ---------------------------------------------------------------------------


def test_identificar_contato_usa_contato_existente():
    contato = {"id": "uuid-contato-1", "telefone": "+5511999990000"}
    mock_buscar = MagicMock(return_value=contato)
    mock_tool = MagicMock()
    mock_tool.invoke = mock_buscar

    with (
        patch("core.nodes.get_supabase"),
        patch("core.nodes._make_buscar_contato", return_value=lambda tid: mock_tool),
    ):
        resultado = identificar_contato(estado(vertical="clinica"))

    assert resultado["contato_id"] == "uuid-contato-1"


def test_identificar_contato_cria_se_nao_existir():
    novo_contato = {"id": "uuid-contato-novo", "telefone": "+5511999990000"}
    mock_buscar_tool = MagicMock()
    mock_buscar_tool.invoke.return_value = None  # não encontrado
    mock_criar_tool = MagicMock()
    mock_criar_tool.invoke.return_value = novo_contato

    with (
        patch("core.nodes.get_supabase"),
        patch("core.nodes._make_buscar_contato", return_value=lambda tid: mock_buscar_tool),
        patch("core.nodes._make_criar_contato", return_value=lambda tid: mock_criar_tool),
    ):
        resultado = identificar_contato(estado(vertical="clinica"))

    assert resultado["contato_id"] == "uuid-contato-novo"
    mock_criar_tool.invoke.assert_called_once_with({"telefone": "+5511999990000"})


def test_identificar_contato_pula_se_contato_id_ja_preenchido():
    """Não deve consultar o banco se contato_id já está no State."""
    with patch("core.nodes.get_supabase") as mock_get:
        resultado = identificar_contato(estado(contato_id="uuid-ja-existe"))

    mock_get.assert_not_called()
    assert resultado == {}


# ---------------------------------------------------------------------------
# deve_usar_tools
# ---------------------------------------------------------------------------


def test_deve_usar_tools_retorna_executar_quando_ha_tool_calls():
    tool_call = {"name": "buscar_contato", "args": {"telefone": "+5511999990000"}, "id": "tc-1"}
    msg = AIMessage(content="", tool_calls=[tool_call])
    assert deve_usar_tools(estado(messages=[msg])) == "executar_tools"


def test_deve_usar_tools_retorna_end_quando_sem_tool_calls():
    msg = AIMessage(content="Tudo certo!")
    assert deve_usar_tools(estado(messages=[msg])) == END


def test_deve_usar_tools_retorna_end_para_mensagem_humana():
    msg = HumanMessage(content="Oi")
    assert deve_usar_tools(estado(messages=[msg])) == END


# ---------------------------------------------------------------------------
# executar_tools
# ---------------------------------------------------------------------------


async def test_executar_tools_invoca_tool_correta_e_retorna_tool_message():
    mock_tool = MagicMock()
    mock_tool.name = "buscar_contato"
    mock_tool.ainvoke = AsyncMock(return_value={"id": "uuid-1", "nome": "João"})

    tool_call = {"name": "buscar_contato", "args": {"telefone": "+5511999990000"}, "id": "tc-abc"}
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [mock_tool]
        resultado = await executar_tools(
            estado(
                messages=[ai_msg],
                vertical="clinica",
                tenant_id="tenant-123",
            )
        )

    assert len(resultado["messages"]) == 1
    tm = resultado["messages"][0]
    assert isinstance(tm, ToolMessage)
    assert tm.tool_call_id == "tc-abc"
    mock_tool.ainvoke.assert_called_once_with({"telefone": "+5511999990000"})


async def test_executar_tools_retorna_erro_para_tool_desconhecida():
    tool_call = {"name": "tool_inexistente", "args": {}, "id": "tc-xyz"}
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = []
        resultado = await executar_tools(estado(messages=[ai_msg], vertical="clinica"))

    tm = resultado["messages"][0]
    assert "não registrada" in tm.content


async def test_executar_tools_captura_excecao_sem_crashar():
    mock_tool = MagicMock()
    mock_tool.name = "buscar_contato"
    mock_tool.ainvoke = AsyncMock(side_effect=RuntimeError("falha de conexão"))

    tool_call = {"name": "buscar_contato", "args": {}, "id": "tc-err"}
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [mock_tool]
        resultado = await executar_tools(estado(messages=[ai_msg], vertical="clinica"))

    assert "Erro ao executar" in resultado["messages"][0].content


async def test_executar_tools_loga_excecao_em_vez_de_engolir_silenciosamente(caplog):
    """Antes desta correção, uma exceção da tool virava só texto no ToolMessage
    devolvido ao LLM — nada aparecia nos logs do servidor."""
    mock_tool = MagicMock()
    mock_tool.name = "transferir_para_atendente"
    mock_tool.ainvoke = AsyncMock(side_effect=RuntimeError("violates foreign key constraint"))

    tool_call = {
        "name": "transferir_para_atendente",
        "args": {"contato_id": "bogus"},
        "id": "tc-err",
    }
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    with patch("core.nodes.registry") as mock_registry, caplog.at_level("ERROR"):
        mock_registry.get_tools.return_value = [mock_tool]
        await executar_tools(
            estado(
                messages=[ai_msg],
                vertical="clinica",
                tenant_config={"handoff": {"habilitado": True}},
            )
        )

    assert any("transferir_para_atendente" in rec.getMessage() for rec in caplog.records)
    assert "violates foreign key constraint" in caplog.text


# ---------------------------------------------------------------------------
# agente
# ---------------------------------------------------------------------------


def test_agente_invoca_llm_e_retorna_ai_message():
    resposta = AIMessage(content="Posso ajudar!")

    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = resposta
        MockLLM.return_value = mock_llm

        resultado = agente(estado(vertical="clinica", system_prompt="Assistente médico."))

    assert resultado["messages"] == [resposta]
    mock_registry.get_tools.assert_called_once_with("clinica", "tenant-123")


def test_agente_usa_system_prompt_padrao_quando_vazio():
    from core.nodes import _SYSTEM_PROMPT_PADRAO

    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(estado(vertical="clinica", system_prompt=""))

    # Verifica que o SystemMessage enviado ao LLM usa o prompt padrão como base
    # e inclui o bloco de contexto (telefone do remetente injetado — commit 4b57fe1)
    prompt_final = _texto_system(mock_llm)
    assert prompt_final.startswith(_SYSTEM_PROMPT_PADRAO)
    assert "CONTEXTO DA CONVERSA" in prompt_final
    assert "+5511999990000" in prompt_final


def test_agente_injeta_contato_id_real_no_prompt():
    """Sem isto, o LLM nunca vê o contato_id resolvido por identificar_contato
    e precisa inventar um valor ao chamar tools que exigem contato_id —
    causa raiz de transferir_para_atendente falhar com FK violation."""
    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(
            estado(
                vertical="clinica",
                system_prompt="Assistente médico.",
                contato_id="c0ffee00-0000-0000-0000-000000000000",
            )
        )

    prompt_final = _texto_system(mock_llm)
    assert "c0ffee00-0000-0000-0000-000000000000" in prompt_final
    assert "nunca invente um id" in prompt_final


# ---------------------------------------------------------------------------
# _tools_disponiveis — feature flag de handoff por tenant
# ---------------------------------------------------------------------------


def test_tools_disponiveis_remove_handoff_quando_flag_ausente():
    """Padrão é opt-in (False) — sem tenant_config.handoff.habilitado, a tool
    não aparece mesmo que o registry a tenha para o vertical."""
    outras = _mock_tool("buscar_contato")
    handoff = _mock_tool("transferir_para_atendente")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, handoff]
        tools = _tools_disponiveis(estado(vertical="clinica", tenant_config={}))

    assert {t.name for t in tools} == {"buscar_contato"}


def test_tools_disponiveis_remove_handoff_quando_flag_explicitamente_false():
    outras = _mock_tool("buscar_contato")
    handoff = _mock_tool("transferir_para_atendente")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, handoff]
        tools = _tools_disponiveis(
            estado(vertical="clinica", tenant_config={"handoff": {"habilitado": False}})
        )

    assert {t.name for t in tools} == {"buscar_contato"}


def test_tools_disponiveis_inclui_handoff_quando_flag_habilitada():
    outras = _mock_tool("buscar_contato")
    handoff = _mock_tool("transferir_para_atendente")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, handoff]
        tools = _tools_disponiveis(
            estado(vertical="clinica", tenant_config={"handoff": {"habilitado": True}})
        )

    assert {t.name for t in tools} == {"buscar_contato", "transferir_para_atendente"}


def test_handoff_tools_constante_bate_com_nome_real_da_tool():
    """Se o nome da tool em tools/verticals/clinica/handoff.py mudar sem
    atualizar HANDOFF_TOOLS, o filtro para de funcionar silenciosamente."""
    from tools.verticals.clinica.handoff import _make_transferir_para_atendente

    tool_real = _make_transferir_para_atendente(MagicMock())("tenant-1")
    assert tool_real.name in HANDOFF_TOOLS


def test_agente_nao_envia_handoff_tool_ao_llm_quando_flag_off():
    handoff = _mock_tool("transferir_para_atendente")
    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = [handoff]
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(estado(vertical="clinica", tenant_config={}))

    tools_passadas = mock_llm.bind_tools.call_args[0][0]
    assert tools_passadas == []


async def test_executar_tools_nao_reconhece_handoff_quando_flag_off():
    """Mesmo que o LLM (de algum jeito) tente chamar a tool, executar_tools
    não deve encontrá-la na lista filtrada — trata como tool desconhecida."""
    handoff = _mock_tool("transferir_para_atendente")
    tool_call = {"name": "transferir_para_atendente", "args": {}, "id": "tc-1"}
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [handoff]
        estado_teste = estado(messages=[ai_msg], vertical="clinica", tenant_config={})
        resultado = await executar_tools(estado_teste)

    assert "não registrada" in resultado["messages"][0].content


# ---------------------------------------------------------------------------
# _tools_disponiveis — feature flag de agenda por tenant
# ---------------------------------------------------------------------------


def _mock_agenda_tools() -> list[MagicMock]:
    return [_mock_tool(nome) for nome in AGENDA_TOOLS]


def test_tools_disponiveis_remove_agenda_quando_flag_ausente():
    """Padrão é opt-in (False) — sem tenant_config.agenda.habilitada, as 4
    tools de agendamento não aparecem mesmo que o registry as tenha."""
    outras = _mock_tool("buscar_contato")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, *_mock_agenda_tools()]
        tools = _tools_disponiveis(estado(vertical="clinica", tenant_config={}))

    assert {t.name for t in tools} == {"buscar_contato"}


def test_tools_disponiveis_remove_agenda_quando_flag_explicitamente_false():
    outras = _mock_tool("buscar_contato")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, *_mock_agenda_tools()]
        tools = _tools_disponiveis(
            estado(vertical="clinica", tenant_config={"agenda": {"habilitada": False}})
        )

    assert {t.name for t in tools} == {"buscar_contato"}


def test_tools_disponiveis_inclui_agenda_quando_flag_habilitada():
    outras = _mock_tool("buscar_contato")
    with patch("core.nodes.registry") as mock_registry:
        mock_registry.get_tools.return_value = [outras, *_mock_agenda_tools()]
        tools = _tools_disponiveis(
            estado(vertical="clinica", tenant_config={"agenda": {"habilitada": True}})
        )

    assert {t.name for t in tools} == {"buscar_contato", *AGENDA_TOOLS}


def test_agenda_tools_constante_bate_com_nomes_reais_das_tools():
    """Se um nome de tool em tools/verticals/clinica/agenda_demo.py mudar sem
    atualizar AGENDA_TOOLS, o filtro para de funcionar silenciosamente."""
    from tools.verticals.clinica.agenda_demo import (
        _make_buscar_agendamentos,
        _make_buscar_slots_disponiveis,
        _make_criar_agendamento,
    )

    client = MagicMock()
    nomes_reais = {
        _make_buscar_slots_disponiveis(client)("tenant-1").name,
        _make_criar_agendamento(client, client)("tenant-1").name,
        _make_buscar_agendamentos(client)("tenant-1").name,
    }
    assert nomes_reais == AGENDA_TOOLS


def test_agente_injeta_instrucao_de_ligar_quando_agenda_e_handoff_desabilitados():
    """Sem handoff, transferir_para_atendente não está no bind_tools deste
    turno — a instrução não pode mandar o agente chamar uma tool ausente."""
    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(estado(vertical="clinica", system_prompt="Assistente médico.", tenant_config={}))

    prompt_final = _texto_system(mock_llm)
    assert "AGENDAMENTO: O agendamento online não está disponível" in prompt_final
    assert "transferir_para_atendente" not in prompt_final


def test_agente_injeta_instrucao_de_transferir_quando_agenda_off_e_handoff_on():
    """Com handoff habilitado, a tool está disponível neste turno — a
    instrução deve mandar o agente transferir em vez de orientar a ligar."""
    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(
            estado(
                vertical="clinica",
                system_prompt="Assistente médico.",
                tenant_config={"handoff": {"habilitado": True}},
            )
        )

    prompt_final = _texto_system(mock_llm)
    assert "transferir_para_atendente" in prompt_final
    assert "Não oriente o paciente a ligar" in prompt_final
    assert "AGENDAMENTO: O agendamento online não está disponível" not in prompt_final


def test_agente_nao_injeta_instrucao_quando_agenda_habilitada():
    with (
        patch("core.nodes.ChatAnthropic") as MockLLM,
        patch("core.nodes.registry") as mock_registry,
    ):
        mock_registry.get_tools.return_value = []
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(content="ok")
        MockLLM.return_value = mock_llm

        agente(
            estado(
                vertical="clinica",
                system_prompt="Assistente médico.",
                tenant_config={"agenda": {"habilitada": True}},
            )
        )

    prompt_final = _texto_system(mock_llm)
    assert "AGENDAMENTO: O agendamento online não está disponível" not in prompt_final
