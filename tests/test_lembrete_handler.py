"""Testes para gateway/lembrete_handler.py."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.base import MensagemRecebida, TipoMensagem
from gateway.lembrete_handler import (
    _buscar_agendamento_recente_lembrete,
    _buscar_metadata_conversa,
    _normalizar,
    _salvar_metadata_conversa,
    handle_resposta_lembrete,
)
from gateway.tenant_resolver import TenantInfo

_TENANT = TenantInfo(tenant_id="t-1", vertical="clinica", canal_id="canal-1")

_PATCH_CORE = "gateway.lembrete_handler.get_supabase"
_PATCH_VERTICAL = "gateway.lembrete_handler.get_vertical_supabase"


def _mensagem(texto: str = "sim", nome: str = "João Silva") -> MensagemRecebida:
    return MensagemRecebida(
        message_id="msg-1",
        numero_destino="551199990000",
        numero_remetente="5567988880000",
        nome_remetente=nome,
        tipo=TipoMensagem.texto,
        texto=texto,
        provider="cloud_api",
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        metadata={},
    )


# ─── _normalizar ──────────────────────────────────────────────────────────────


def test_normalizar_remove_acento() -> None:
    assert _normalizar("NÃO") == "nao"


def test_normalizar_lowercases() -> None:
    assert _normalizar("SIM") == "sim"


def test_normalizar_strip_espacos() -> None:
    assert _normalizar("  ok  ") == "ok"


def test_normalizar_preserva_espacos_internos() -> None:
    assert _normalizar("tudo bem") == "tudo bem"


# ─── _buscar_agendamento_recente_lembrete ─────────────────────────────────────


def _mock_vertical_lembrete(rows: list[dict]) -> MagicMock:
    """Chain: .select().eq().eq().eq().gte().order().limit().execute().data"""
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.gte.return_value.order.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return m


def _mock_core_metadata(rows: list[dict]) -> MagicMock:
    """Chain: .select().eq().limit().execute().data"""
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return m


def test_buscar_agendamento_retorna_primeiro_resultado() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "convenio": "UNIMED",
        "status": "confirmado",
    }
    vertical = _mock_vertical_lembrete([ag])
    result = _buscar_agendamento_recente_lembrete(vertical, "t-1", "ct-1")
    assert result == ag


def test_buscar_agendamento_sem_resultado_retorna_none() -> None:
    vertical = _mock_vertical_lembrete([])
    assert _buscar_agendamento_recente_lembrete(vertical, "t-1", "ct-1") is None


# ─── _buscar_metadata_conversa ────────────────────────────────────────────────


def test_buscar_metadata_retorna_dict() -> None:
    core = _mock_core_metadata(
        [{"metadata": {"aguardando_remarcacao": True, "agendamento_id": "ag-1"}}]
    )
    meta = _buscar_metadata_conversa(core, "conv-1")
    assert meta["aguardando_remarcacao"] is True


def test_buscar_metadata_sem_conversa_retorna_vazio() -> None:
    core = _mock_core_metadata([])
    assert _buscar_metadata_conversa(core, "conv-1") == {}


def test_buscar_metadata_none_retorna_vazio() -> None:
    core = _mock_core_metadata([{"metadata": None}])
    assert _buscar_metadata_conversa(core, "conv-1") == {}


# ─── _salvar_metadata_conversa ───────────────────────────────────────────────


def test_salvar_metadata_chama_update_com_payload_correto() -> None:
    core = MagicMock()
    _salvar_metadata_conversa(core, "t-1", "conv-1", {"aguardando_remarcacao": True})
    core.table.assert_called_with("conversas")
    payload = core.table.return_value.update.call_args[0][0]
    assert payload["metadata"] == {"aguardando_remarcacao": True}


# ─── handle_resposta_lembrete ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_sem_contato_id_retorna_none() -> None:
    result = await handle_resposta_lembrete(_mensagem(), _TENANT, None, "conv-1")
    assert result is None


@pytest.mark.asyncio
async def test_handle_sem_conversa_id_retorna_none() -> None:
    result = await handle_resposta_lembrete(_mensagem(), _TENANT, "ct-1", None)
    assert result is None


@pytest.mark.asyncio
async def test_handle_texto_vazio_retorna_none() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([])
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(_mensagem(texto=""), _TENANT, "ct-1", "conv-1")
    assert result is None


@pytest.mark.asyncio
async def test_handle_sem_lembrete_recente_retorna_none() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([])
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(_mensagem("sim"), _TENANT, "ct-1", "conv-1")
    assert result is None


@pytest.mark.asyncio
async def test_handle_sim_confirma_agendamento() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "convenio": None,
        "status": "confirmado",
    }
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([ag])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(_mensagem("sim"), _TENANT, "ct-1", "conv-1")

    assert result is not None
    assert "confirmada" in result.lower()
    vertical.table.return_value.update.assert_called_once()
    payload = vertical.table.return_value.update.call_args[0][0]
    assert payload["status"] == "confirmado_paciente"


@pytest.mark.asyncio
async def test_handle_sim_usa_primeiro_nome() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "convenio": None,
        "status": "confirmado",
    }
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([ag])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(
            _mensagem("sim", nome="Maria Souza"), _TENANT, "ct-1", "conv-1"
        )

    assert "Maria" in result
    assert "Souza" not in result


@pytest.mark.asyncio
async def test_handle_aguardando_remarcacao_recusa_limpa_estado() -> None:
    core = _mock_core_metadata(
        [{"metadata": {"aguardando_remarcacao": True, "agendamento_id": "ag-1"}}]
    )
    vertical = _mock_vertical_lembrete([])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(_mensagem("não"), _TENANT, "ct-1", "conv-1")

    assert result is not None
    assert "disposição" in result.lower()
    core.table.return_value.update.assert_called_once()
    payload = core.table.return_value.update.call_args[0][0]
    assert payload["metadata"] == {}


@pytest.mark.asyncio
async def test_handle_aguardando_remarcacao_escolha_passa_para_langgraph() -> None:
    core = _mock_core_metadata(
        [{"metadata": {"aguardando_remarcacao": True, "agendamento_id": "ag-1"}}]
    )
    vertical = _mock_vertical_lembrete([])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(
            _mensagem("quero o de segunda as 09h"), _TENANT, "ct-1", "conv-1"
        )

    assert result is None
    core.table.return_value.update.assert_called_once()


@pytest.mark.asyncio
async def test_handle_texto_irrelevante_retorna_none() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "convenio": None,
        "status": "confirmado",
    }
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([ag])

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(
            _mensagem("qual o endereço da clínica?"), _TENANT, "ct-1", "conv-1"
        )

    assert result is None


# ─── Itens 3.2 e 3.3: botões do template e remarcação sem cancelar ───────────

_AG = {
    "id": "ag-1",
    "data_hora": "2026-10-30T18:00:00+00:00",
    "convenio": None,
    "status": "confirmado",
}
_PATCH_TRANSFERIR = "tools.verticals.clinica.handoff._make_transferir_para_atendente"


@pytest.fixture(autouse=True)
def _fuso_campo_grande():
    """Clínica em UTC-4 (o fuso vem de tenants.config.lembrete.utc_offset_horas)."""
    from datetime import timedelta

    with patch(
        "gateway.lembrete_handler._fuso_da_clinica",
        return_value=timezone(timedelta(hours=-4)),
    ):
        yield


def _mensagem_botao(payload: str, titulo: str) -> MensagemRecebida:
    m = _mensagem(titulo)
    return m.model_copy(
        update={"tipo": TipoMensagem.botao_resposta, "metadata": {"button_id": payload}}
    )


def _mock_vertical_botao(rows: list[dict]) -> MagicMock:
    """Chain do botão: .select().eq(id).eq(tenant).eq(contato).limit().execute().data"""
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return m


def _mock_transferir(texto: str = "Perfeito! Encaminhei você para a nossa equipe.") -> MagicMock:
    tool = MagicMock()
    tool.ainvoke = AsyncMock(return_value=texto)
    factory = MagicMock(return_value=MagicMock(return_value=tool))
    factory.tool = tool
    return factory


@pytest.mark.asyncio
async def test_nao_por_texto_pede_remarcacao_e_nao_cancela() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([_AG])
    transferir = _mock_transferir()
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_TRANSFERIR, transferir),
    ):
        result = await handle_resposta_lembrete(_mensagem("não"), _TENANT, "ct-1", "conv-1")

    assert "continua reservada" in result
    assert "sexta-feira, 30/10 às 14h00" in result
    assert "Encaminhei" in result
    vertical.table.return_value.update.assert_not_called()
    args = transferir.tool.ainvoke.call_args.args[0]
    assert args["contato_id"] == "ct-1"
    assert args["role_destino"] == "atendente"
    assert "Remarcar consulta de sexta-feira, 30/10 às 14h00" == args["motivo"]


@pytest.mark.asyncio
async def test_remarcar_por_texto_tambem_vale() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([_AG])
    transferir = _mock_transferir()
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_TRANSFERIR, transferir),
    ):
        result = await handle_resposta_lembrete(
            _mensagem("preciso remarcar"), _TENANT, "ct-1", "conv-1"
        )
    assert result is not None
    transferir.tool.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_botao_confirmar_confirma_a_consulta_do_payload() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_botao([_AG])
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
    ):
        result = await handle_resposta_lembrete(
            _mensagem_botao("lembrete:confirmar:ag-1", "Confirmar presença"),
            _TENANT,
            "ct-1",
            "conv-1",
        )
    assert "confirmada" in result.lower()
    select = vertical.table.return_value.select.return_value
    select.eq.assert_called_with("id", "ag-1")
    select.eq.return_value.eq.assert_called_with("tenant_id", "t-1")
    select.eq.return_value.eq.return_value.eq.assert_called_with("contato_id", "ct-1")
    payload = vertical.table.return_value.update.call_args[0][0]
    assert payload == {"status": "confirmado_paciente"}


@pytest.mark.asyncio
async def test_botao_remarcar_faz_handoff_sem_cancelar() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_botao([_AG])
    transferir = _mock_transferir()
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_TRANSFERIR, transferir),
    ):
        result = await handle_resposta_lembrete(
            _mensagem_botao("lembrete:remarcar:ag-1", "Preciso remarcar"),
            _TENANT,
            "ct-1",
            "conv-1",
        )
    assert "continua reservada" in result
    vertical.table.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_botao_de_consulta_de_outro_paciente_ou_cancelada_nao_age() -> None:
    for rows in ([], [{**_AG, "status": "cancelado_clinica"}]):
        core = _mock_core_metadata([{"metadata": {}}])
        vertical = _mock_vertical_botao(rows)
        transferir = _mock_transferir()
        with (
            patch(_PATCH_CORE, return_value=core),
            patch(_PATCH_VERTICAL, return_value=vertical),
            patch(_PATCH_TRANSFERIR, transferir),
        ):
            result = await handle_resposta_lembrete(
                _mensagem_botao("lembrete:confirmar:ag-x", "Confirmar presença"),
                _TENANT,
                "ct-1",
                "conv-1",
            )
        assert "Não encontrei essa consulta" in result
        vertical.table.return_value.update.assert_not_called()
        transferir.tool.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_falha_no_handoff_ainda_responde_e_nao_cancela() -> None:
    core = _mock_core_metadata([{"metadata": {}}])
    vertical = _mock_vertical_lembrete([_AG])
    transferir = _mock_transferir()
    transferir.tool.ainvoke.side_effect = RuntimeError("db fora")
    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch(_PATCH_TRANSFERIR, transferir),
    ):
        result = await handle_resposta_lembrete(_mensagem("não"), _TENANT, "ct-1", "conv-1")
    assert "continua reservada" in result
    vertical.table.return_value.update.assert_not_called()
