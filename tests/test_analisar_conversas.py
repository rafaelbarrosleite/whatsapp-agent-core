"""Regressão do loop de re-análise (custo recorrente com Sonnet).

O bug: a marca d'água era conversas.updated_at, mas gravar o metadata da própria
análise dispara o trigger conversas_updated_at, que bumpa updated_at alguns ms
DEPOIS do timestamp gerado no Python. A conversa parecia "modificada desde a
última análise" para sempre e era reanalisada a cada 10 min, indefinidamente.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import jobs.analisar_conversas as job

_PATCH_LLM = "jobs.analisar_conversas.ChatAnthropic"


def _saida_llm(analise):
    """Formato de with_structured_output(include_raw=True): resposta crua + objeto."""
    return {"raw": None, "parsed": analise, "parsing_error": None}


def _analise_vazia() -> MagicMock:
    a = MagicMock()
    a.consulta_confirmada = False
    a.paciente_terceiro = False
    a.em_espera = False
    a.model_dump.return_value = {}
    return a


def _core_com_mensagens(mensagens: list[dict]) -> MagicMock:
    """core.table('mensagens')...execute().data == mensagens; update() no-op."""
    core = MagicMock()
    (
        core.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data
    ) = mensagens
    return core


def _conversa(meta: dict | None = None) -> dict:
    return {
        "id": "conv-1",
        "tenant_id": "t-1",
        "contato_id": "c-1",
        "metadata": meta or {},
        # updated_at SEMPRE mais novo que a marca d'água — é exatamente o que o
        # trigger do banco produz e o que quebrava a lógica antiga.
        "updated_at": "2026-07-28T12:49:23.555871+00:00",
    }


_MSGS = [
    {"papel": "usuario", "conteudo": "oi", "created_at": "2026-07-28T12:00:00+00:00"},
    {"papel": "atendente", "conteudo": "olá", "created_at": "2026-07-28T12:49:23.519643+00:00"},
]


async def test_pula_quando_nao_ha_mensagem_nova_e_nao_chama_llm():
    """Marca d'água == created_at da última mensagem → NÃO chama o LLM.
    Com a lógica antiga (updated_at) isto reanalisava para sempre."""
    core = _core_com_mensagens(_MSGS)
    conv = _conversa({job._MARCA_DAGUA: "2026-07-28T12:49:23.519643+00:00"})

    with patch(_PATCH_LLM) as mock_llm:
        analisou = await job._analisar_uma(core, MagicMock(), conv, {}, dry_run=False)

    assert analisou is False
    mock_llm.assert_not_called()  # nenhum token gasto


async def test_analisa_quando_chega_mensagem_nova():
    core = _core_com_mensagens(_MSGS)
    conv = _conversa({job._MARCA_DAGUA: "2026-07-28T11:00:00+00:00"})  # anterior

    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=_saida_llm(_analise_vazia())
    )
    with patch(_PATCH_LLM, return_value=llm):
        analisou = await job._analisar_uma(core, MagicMock(), conv, {}, dry_run=False)

    assert analisou is True


async def test_primeira_analise_sem_marca_dagua_roda():
    core = _core_com_mensagens(_MSGS)
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=_saida_llm(_analise_vazia())
    )
    with patch(_PATCH_LLM, return_value=llm):
        analisou = await job._analisar_uma(core, MagicMock(), _conversa(), {}, dry_run=False)

    assert analisou is True


async def test_grava_marca_dagua_com_created_at_da_ultima_mensagem():
    """A marca gravada tem que ser o created_at da mensagem — não now() —
    senão o trigger do banco volta a vencer a comparação."""
    core = _core_com_mensagens(_MSGS)
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=_saida_llm(_analise_vazia())
    )
    with patch(_PATCH_LLM, return_value=llm):
        await job._analisar_uma(core, MagicMock(), _conversa(), {}, dry_run=False)

    payload = core.table.return_value.update.call_args[0][0]
    assert payload["metadata"][job._MARCA_DAGUA] == "2026-07-28T12:49:23.519643+00:00"


async def test_idempotente_segunda_rodada_nao_chama_llm():
    """Simula duas rodadas seguidas do scheduler: a 2ª não pode gastar token."""
    core = _core_com_mensagens(_MSGS)
    conv = _conversa()
    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=_saida_llm(_analise_vazia())
    )

    with patch(_PATCH_LLM, return_value=llm):
        assert await job._analisar_uma(core, MagicMock(), conv, {}, dry_run=False) is True
        # o metadata que teria sido persistido alimenta a rodada seguinte
        meta_persistido = core.table.return_value.update.call_args[0][0]["metadata"]
        conv_2 = _conversa(meta_persistido)
        assert await job._analisar_uma(core, MagicMock(), conv_2, {}, dry_run=False) is False


def test_usa_haiku_e_nao_sonnet():
    """Extração estruturada roda em Haiku 4.5 ($1/M) e não Sonnet 4.6 ($3/M)."""
    assert job._MODEL == "claude-haiku-4-5"


def test_ia_analise_desligada_por_padrao():
    from jobs.analisar_conversas import _ia_ativa

    assert _ia_ativa(None) is False
    assert _ia_ativa({}) is False
    assert _ia_ativa({"notificacoes": {}}) is False
    assert _ia_ativa({"notificacoes": {"ia_analise": False}}) is False
    assert _ia_ativa({"notificacoes": {"ia_analise": True}}) is True
