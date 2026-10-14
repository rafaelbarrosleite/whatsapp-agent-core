"""Economia de tokens no nó agente (CLAUDE.md §5): prompt caching + janela
deslizante de histórico."""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from core.nodes import _MAX_MSGS_HISTORICO, _historico_para_llm, agente

_PATCH_LLM = "core.nodes.ChatAnthropic"


def _state(**extra) -> dict:
    base = {
        "messages": [HumanMessage(content="oi")],
        "tenant_id": "t-1",
        "vertical": "clinica",
        "telefone": "+5567999991234",
        "contato_id": "c-1",
        "system_prompt": "PROMPT DO TENANT " * 20,
        "tenant_config": {},
    }
    base.update(extra)
    return base


def _mock_llm():
    """ChatAnthropic mockado; devolve o `mensagens` recebido pelo invoke."""
    resposta = AIMessage(content="ok")
    llm = MagicMock()
    llm.bind_tools.return_value.invoke.return_value = resposta
    return llm


def _system_enviado(llm) -> SystemMessage:
    return llm.bind_tools.return_value.invoke.call_args[0][0][0]


# ─── Prompt caching ──────────────────────────────────────────────────────────


def test_system_e_dividido_em_bloco_estavel_e_volatil():
    llm = _mock_llm()
    with (
        patch(_PATCH_LLM, return_value=llm),
        patch("core.nodes._tools_disponiveis", return_value=[]),
    ):
        agente(_state())

    blocos = _system_enviado(llm).content
    assert isinstance(blocos, list) and len(blocos) == 2
    assert blocos[0]["cache_control"] == {"type": "ephemeral"}  # breakpoint no estável
    assert "cache_control" not in blocos[1]  # volátil fora do cache


def test_bloco_cacheado_nao_contem_nada_volatil():
    """O invariante que faz o cache render: telefone/contato_id/data no bloco
    estável fariam cada paciente (e cada dia) virar uma entrada de cache."""
    llm = _mock_llm()
    with (
        patch(_PATCH_LLM, return_value=llm),
        patch("core.nodes._tools_disponiveis", return_value=[]),
    ):
        agente(_state())

    estavel, volatil = _system_enviado(llm).content
    assert "+5567999991234" not in estavel["text"]
    assert "c-1" not in estavel["text"]
    assert "PROMPT DO TENANT" in estavel["text"]
    # e o volátil segue carregando o contexto do paciente
    assert "+5567999991234" in volatil["text"]
    assert "c-1" in volatil["text"]


def test_bloco_estavel_identico_para_pacientes_diferentes():
    """Dois pacientes do mesmo tenant compartilham a MESMA entrada de cache."""
    textos = []
    for tel, cid in (("+5511111111111", "c-1"), ("+5522222222222", "c-2")):
        llm = _mock_llm()
        with (
            patch(_PATCH_LLM, return_value=llm),
            patch("core.nodes._tools_disponiveis", return_value=[]),
        ):
            agente(_state(telefone=tel, contato_id=cid))
        textos.append(_system_enviado(llm).content[0]["text"])

    assert textos[0] == textos[1]


def test_instrucao_de_feature_flag_fica_no_bloco_cacheado():
    """Instrução de agenda desabilitada é estável por tenant — tem que entrar no
    bloco cacheado, não no volátil (senão encurta o prefixo cacheável)."""
    llm = _mock_llm()
    state = _state(tenant_config={"agenda_habilitada": False, "handoff_habilitado": False})
    with (
        patch(_PATCH_LLM, return_value=llm),
        patch("core.nodes._tools_disponiveis", return_value=[]),
    ):
        agente(state)

    estavel, volatil = _system_enviado(llm).content
    assert len(estavel["text"]) > len(state["system_prompt"])  # instrução foi anexada
    assert "CONTEXTO DA CONVERSA" in volatil["text"]


# ─── Janela deslizante de histórico ──────────────────────────────────────────


def test_historico_curto_passa_intacto():
    msgs = [HumanMessage(content=f"m{i}") for i in range(5)]
    assert _historico_para_llm(msgs) == msgs


def test_historico_longo_e_podado():
    msgs = []
    for i in range(60):
        msgs.append(HumanMessage(content=f"pergunta {i}"))
        msgs.append(AIMessage(content=f"resposta {i}"))

    podado = _historico_para_llm(msgs)
    assert len(podado) < len(msgs)
    assert len(podado) <= _MAX_MSGS_HISTORICO
    assert podado[-1] is msgs[-1]  # mantém o fim da conversa
    assert isinstance(podado[0], HumanMessage)  # nunca começa em tool_result órfão


def test_janela_nunca_comeca_em_tool_message_orfa():
    """ToolMessage sem o AIMessage(tool_calls) que a originou faz a API rejeitar
    o turno — start_on='human' tem que descartar essa borda."""
    msgs = []
    for i in range(40):
        msgs.append(HumanMessage(content=f"p{i}"))
        msgs.append(
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": f"call-{i}"}])
        )
        msgs.append(ToolMessage(content="r", tool_call_id=f"call-{i}"))

    podado = _historico_para_llm(msgs)
    assert not isinstance(podado[0], ToolMessage)


def test_poda_com_falha_devolve_historico_completo():
    msgs = [HumanMessage(content=f"m{i}") for i in range(_MAX_MSGS_HISTORICO + 10)]
    with patch("core.nodes.trim_messages", side_effect=Exception("boom")):
        assert _historico_para_llm(msgs) == msgs


def test_poda_vazia_devolve_historico_completo():
    """Se a janela zerar (ex.: nenhum HumanMessage no fim), manda tudo em vez de
    enviar zero mensagens — a API rejeita requisição sem mensagens."""
    msgs = [HumanMessage(content=f"m{i}") for i in range(_MAX_MSGS_HISTORICO + 10)]
    with patch("core.nodes.trim_messages", return_value=[]):
        assert _historico_para_llm(msgs) == msgs


def test_agente_envia_historico_podado_ao_llm():
    msgs = []
    for i in range(60):
        msgs.append(HumanMessage(content=f"p{i}"))
        msgs.append(AIMessage(content=f"r{i}"))

    llm = _mock_llm()
    with (
        patch(_PATCH_LLM, return_value=llm),
        patch("core.nodes._tools_disponiveis", return_value=[]),
    ):
        agente(_state(messages=msgs))

    enviadas = llm.bind_tools.return_value.invoke.call_args[0][0]
    assert len(enviadas) - 1 <= _MAX_MSGS_HISTORICO  # -1 do SystemMessage
