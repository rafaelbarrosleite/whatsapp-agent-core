from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobs.lembrete_consulta import (
    _calcular_janela_amanha,
    _formatar_mensagem,
    _fuso,
    _lembrete_habilitado,
    _tenants_com_lembrete,
    enviar_lembretes_do_dia,
)

_CAMPO_GRANDE = timezone(timedelta(hours=-4))
_UTC = timezone.utc


_CONFIG_T1 = {"notificacoes": {"lembrete": True}, "lembrete": {"utc_offset_horas": -4}}


@pytest.fixture(autouse=True)
def _consentimento_transacional_ativo():
    with patch("jobs.lembrete_consulta.communication_allowed", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _tenant_com_lembrete_ligado():
    """Os testes de envio assumem a clínica t-1 com lembrete ligado em UTC-4."""
    with patch(
        "jobs.lembrete_consulta._tenants_com_lembrete", return_value={"t-1": _CONFIG_T1}
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# _calcular_janela_amanha
# ---------------------------------------------------------------------------


def test_calcular_janela_amanha_retorna_duas_strings_iso() -> None:
    inicio, fim = _calcular_janela_amanha()
    # Ambos devem ser parsáveis como ISO datetime com timezone
    dt_inicio = datetime.fromisoformat(inicio)
    dt_fim = datetime.fromisoformat(fim)
    assert dt_inicio.tzinfo is not None
    assert dt_fim.tzinfo is not None


def test_calcular_janela_amanha_inicio_menor_que_fim() -> None:
    inicio, fim = _calcular_janela_amanha()
    assert datetime.fromisoformat(inicio) < datetime.fromisoformat(fim)


def test_calcular_janela_amanha_diferenca_e_quase_um_dia() -> None:
    inicio, fim = _calcular_janela_amanha()
    diff = datetime.fromisoformat(fim) - datetime.fromisoformat(inicio)
    # 23h59m59s = 86399 segundos
    assert diff.total_seconds() == pytest.approx(86399, abs=1)


def test_calcular_janela_amanha_esta_em_utc() -> None:
    inicio, fim = _calcular_janela_amanha()
    dt_inicio = datetime.fromisoformat(inicio)
    # UTC offset deve ser +00:00
    assert dt_inicio.utcoffset() == timedelta(0)


def test_calcular_janela_amanha_e_dia_seguinte_ao_de_hoje() -> None:
    inicio, _ = _calcular_janela_amanha(_CAMPO_GRANDE)
    hoje_br = datetime.now(tz=_CAMPO_GRANDE).date()
    amanha_br = hoje_br + timedelta(days=1)
    # inicio em UTC deve corresponder à meia-noite de amanhã na clínica (UTC-4)
    dt_inicio_utc = datetime.fromisoformat(inicio)
    dt_inicio_br = dt_inicio_utc.astimezone(_CAMPO_GRANDE)
    assert dt_inicio_br.date() == amanha_br
    assert dt_inicio_br.hour == 0
    assert dt_inicio_br.minute == 0


# ---------------------------------------------------------------------------
# _formatar_mensagem
# ---------------------------------------------------------------------------


def test_formatar_mensagem_usa_primeiro_nome() -> None:
    msg = _formatar_mensagem("Maria Souza", "10h00")
    assert "Maria" in msg
    assert "Souza" not in msg


def test_formatar_mensagem_inclui_horario() -> None:
    msg = _formatar_mensagem("João", "14h30")
    assert "14h30" in msg


def test_formatar_mensagem_nome_vazio_usa_paciente() -> None:
    msg = _formatar_mensagem("", "09h00")
    assert "paciente" in msg


def test_formatar_mensagem_nome_none_equivalente_usa_paciente() -> None:
    msg = _formatar_mensagem("", "09h00")
    assert "paciente" in msg


def test_formatar_mensagem_padrao_nao_cita_clinica_especifica() -> None:
    msg = _formatar_mensagem("Carlos", "08h00")
    assert "Dr. Fulano" not in msg
    assert "Rua das Flores" not in msg


def test_formatar_mensagem_usa_modelo_da_clinica() -> None:
    modelo = "Oi {primeiro_nome}, consulta amanhã às {horario} na Clínica X."
    assert _formatar_mensagem("Ana Lima", "09h30", modelo) == (
        "Oi Ana, consulta amanhã às 09h30 na Clínica X."
    )


def test_formatar_mensagem_modelo_invalido_cai_no_padrao() -> None:
    msg = _formatar_mensagem("Ana", "09h30", "Oi {nome_errado}")
    assert "Ana" in msg and "09h30" in msg


def test_formatar_mensagem_contem_instrucoes_sim_remarcar() -> None:
    """Item 3.3: o texto não oferece cancelar; o paciente pede para remarcar."""
    msg = _formatar_mensagem("Ana", "11h00")
    assert "SIM" in msg
    assert "REMARCAR" in msg
    assert "cancelar" not in msg.lower()


# ---------------------------------------------------------------------------
# enviar_lembretes_do_dia — comportamento integrado (mocks)
# ---------------------------------------------------------------------------


def _mock_vertical(agendamentos: list[dict]) -> MagicMock:
    # Chain: .select().eq(status).eq(lembrete_enviado).eq(tenant_id).gte().lte().execute()
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.gte.return_value.lte.return_value.execute.return_value.data
    ) = agendamentos
    return m


def _mock_core(contatos: list[dict]) -> MagicMock:
    # Chain: .select().eq(id).eq(tenant_id).limit().execute()
    m = MagicMock()
    (
        m.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = contatos
    return m


@pytest.mark.asyncio
async def test_sem_agendamentos_nao_chama_enviar() -> None:
    vertical = _mock_vertical([])
    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=MagicMock()),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()
    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_contato_sem_telefone_pula_e_nao_envia() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "João", "telefone": None}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()

    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_contato_nao_encontrado_pula() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([])  # nenhum contato encontrado

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()

    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_envio_bem_sucedido_marca_lembrete_enviado() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Maria Silva", "telefone": "+5567999990000"}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await enviar_lembretes_do_dia()

    vertical.table.return_value.update.assert_called_once()
    payload = vertical.table.return_value.update.call_args[0][0]
    assert payload["lembrete_enviado"] is True
    assert "lembrete_enviado_em" in payload


@pytest.mark.asyncio
async def test_envio_falho_nao_marca_lembrete_enviado() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Carlos", "telefone": "+5567888880000"}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await enviar_lembretes_do_dia()

    vertical.table.return_value.update.assert_not_called()


@pytest.mark.asyncio
async def test_envio_usa_telefone_do_contato() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Ana", "telefone": "+5567912345678"}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()

    chamada = mock_envia.call_args
    assert chamada.kwargs["numero_destino"] == "+5567912345678"


@pytest.mark.asyncio
async def test_horario_exibido_em_campo_grande() -> None:
    """data_hora armazenada em UTC → exibição deve ser em UTC-4."""
    # 14:00 UTC = 10:00 em UTC-4
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Pedro", "telefone": "+5567999990000"}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()

    texto_enviado = mock_envia.call_args.kwargs["texto"]
    assert "10h00" in texto_enviado


@pytest.mark.asyncio
async def test_excecao_em_contato_nao_para_loop() -> None:
    """Erro ao buscar um contato não deve interromper os demais agendamentos."""
    ag1 = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    ag2 = {
        "id": "ag-2",
        "data_hora": "2026-06-12T16:00:00+00:00",
        "contato_id": "ct-2",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag1, ag2])

    core = MagicMock()
    # Primeiro agendamento lança exceção; segundo retorna contato válido
    core.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.side_effect = [
        Exception("DB temporariamente indisponível"),
        MagicMock(data=[{"nome": "Lucia", "telefone": "+5567988880000"}]),
    ]

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()

    # Segundo agendamento deve ter sido processado mesmo com falha no primeiro
    mock_envia.assert_called_once()


# ---------------------------------------------------------------------------
# Liga/desliga por clínica (padrão desligado) e fuso por clínica
# ---------------------------------------------------------------------------


def test_lembrete_desligado_por_padrao() -> None:
    assert _lembrete_habilitado(None) is False
    assert _lembrete_habilitado({}) is False
    assert _lembrete_habilitado({"notificacoes": {}}) is False
    assert _lembrete_habilitado({"notificacoes": {"lembrete": "true"}}) is False
    assert _lembrete_habilitado({"notificacoes": {"lembrete": True}}) is True


def test_tenants_com_lembrete_filtra_so_os_ligados() -> None:
    core = MagicMock()
    core.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "ligado", "config": {"notificacoes": {"lembrete": True}}},
        {"id": "sem-flag", "config": {"notificacoes": {"ia_analise": False}}},
        {"id": "sem-config", "config": None},
    ]
    assert list(_tenants_com_lembrete(core)) == ["ligado"]


def test_fuso_padrao_brasilia_e_offset_invalido_ignorado() -> None:
    assert _fuso({}).utcoffset(None) == timedelta(hours=-3)
    assert _fuso({"lembrete": {"utc_offset_horas": -4}}).utcoffset(None) == timedelta(hours=-4)
    assert _fuso({"lembrete": {"utc_offset_horas": "x"}}).utcoffset(None) == timedelta(hours=-3)
    assert _fuso({"lembrete": {"utc_offset_horas": 99}}).utcoffset(None) == timedelta(hours=-3)


@pytest.mark.asyncio
async def test_nenhuma_clinica_ligada_nao_consulta_agendamentos(
    _tenant_com_lembrete_ligado,
) -> None:
    _tenant_com_lembrete_ligado.return_value = {}
    vertical = _mock_vertical([{"id": "ag-1"}])
    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=MagicMock()),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant", new_callable=AsyncMock
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()
    vertical.table.assert_not_called()
    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_consulta_filtra_pela_clinica_e_usa_modelo_dela(
    _tenant_com_lembrete_ligado,
) -> None:
    _tenant_com_lembrete_ligado.return_value = {
        "t-1": {**_CONFIG_T1, "lembrete": {"utc_offset_horas": -4, "mensagem": "X {horario}"}}
    }
    ag = {
        "id": "ag-1",
        "data_hora": "2026-06-12T14:00:00+00:00",
        "contato_id": "c",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Ana", "telefone": "+5567912345678"}])
    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_envia,
    ):
        await enviar_lembretes_do_dia()
    filtro_tenant = (
        vertical.table.return_value.select.return_value.eq.return_value.eq.return_value.eq
    )
    filtro_tenant.assert_any_call("tenant_id", "t-1")
    assert mock_envia.call_args.kwargs["texto"] == "X 10h00"


# ---------------------------------------------------------------------------
# Item 3.1/3.2: lembrete por template da Meta, com botões
# ---------------------------------------------------------------------------


def test_parametros_do_template_dia_por_extenso_e_uma_linha() -> None:
    from jobs.lembrete_consulta import parametros_do_template

    dt = datetime(2026, 10, 30, 14, 0, tzinfo=_CAMPO_GRANDE)
    assert parametros_do_template("  Ana  Souza ", "Clínica\nOrto", dt) == [
        "Ana",
        "Clínica Orto",
        "sexta-feira, 30/10",
        "14h00",
    ]


@pytest.mark.asyncio
async def test_com_template_configurado_envia_template_com_botoes(
    _tenant_com_lembrete_ligado,
) -> None:
    config = {
        **_CONFIG_T1,
        "_nome": "Tenant Nome",
        "templates": {"lembrete_consulta": {"nome": "lembrete_consulta", "idioma": "pt_BR"}},
    }
    _tenant_com_lembrete_ligado.return_value = {"t-1": config}
    ag = {
        "id": "ag-1",
        "data_hora": "2026-10-30T18:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    # nome da clínica no cabeçalho dos documentos (tenant_config 'documentos')
    (
        vertical.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [{"valor": {"clinica_nome": "Clínica Ortopédica"}}]
    core = _mock_core([{"nome": "Ana Souza", "telefone": "+5567988880000"}])

    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_template_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_template,
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant", new_callable=AsyncMock
        ) as mock_texto,
    ):
        await enviar_lembretes_do_dia()

    mock_texto.assert_not_called()
    kwargs = mock_template.call_args.kwargs
    assert kwargs["nome"] == "lembrete_consulta"
    assert kwargs["idioma"] == "pt_BR"
    assert kwargs["parametros"] == ["Ana", "Clínica Ortopédica", "sexta-feira, 30/10", "14h00"]
    assert kwargs["payloads_botoes"] == ["lembrete:confirmar:ag-1", "lembrete:remarcar:ag-1"]
    assert kwargs["numero_destino"] == "+5567988880000"


@pytest.mark.asyncio
async def test_sem_template_continua_no_texto_livre() -> None:
    ag = {
        "id": "ag-1",
        "data_hora": "2026-10-30T18:00:00+00:00",
        "contato_id": "ct-1",
        "tenant_id": "t-1",
    }
    vertical = _mock_vertical([ag])
    core = _mock_core([{"nome": "Ana", "telefone": "+5567988880000"}])
    with (
        patch("jobs.lembrete_consulta.get_vertical_supabase", return_value=vertical),
        patch("jobs.lembrete_consulta.get_supabase", return_value=core),
        patch(
            "jobs.lembrete_consulta.enviar_template_tenant", new_callable=AsyncMock
        ) as mock_template,
        patch(
            "jobs.lembrete_consulta.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_texto,
    ):
        await enviar_lembretes_do_dia()
    mock_template.assert_not_called()
    mock_texto.assert_awaited_once()
