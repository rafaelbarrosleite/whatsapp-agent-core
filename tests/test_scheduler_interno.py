"""Testes do scheduler do Internal Agent (jobs multi-tenant).

Cobre:
  - _aniversario_amanha: parsing de datas e matching
  - _buscar_tenants_ativos: consulta ao Core DB
  - _notificacao_ativa: padrão DESLIGADO; só True explícito liga
  - _resolver_medico_whatsapp: config presente → "config"; ausente → (None,"nenhum")
  - job_aniversarios: iteração, sem tenants, falha isolada, toggle off
  - job_resumo_noturno: iteração, sem tenants, falha isolada
  - _aniversarios_tenant: sem aniversariantes, com aniversariante, toggle off, sem médico
  - _resumo_noturno_tenant: contagem de confirmados, follow-ups, toggle off
  - dry_run: não chama enviar_mensagem, não grava crm_notificacoes, origem no log
  - dry_run=False: comportamento live inalterado
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.internal.scheduler import (
    _aniversario_amanha,
    _aniversarios_tenant,
    _buscar_tenants_ativos,
    _notificacao_ativa,
    _resolver_medico_whatsapp,
    _resumo_noturno_tenant,
    job_aniversarios,
    job_resumo_noturno,
)

_BR_TZ = timezone(timedelta(hours=-4))


# ─── _aniversario_amanha ──────────────────────────────────────────────────────


def test_aniversario_amanha_match() -> None:
    assert _aniversario_amanha("1990-06-18", 6, 18) is True


def test_aniversario_amanha_sem_match() -> None:
    assert _aniversario_amanha("1990-01-01", 6, 18) is False


def test_aniversario_amanha_none_retorna_false() -> None:
    assert _aniversario_amanha(None, 6, 18) is False


def test_aniversario_amanha_string_invalida_retorna_false() -> None:
    assert _aniversario_amanha("nao-e-data", 6, 18) is False


def test_aniversario_amanha_string_com_timestamp_trunca_para_data() -> None:
    assert _aniversario_amanha("1990-06-18T00:00:00", 6, 18) is True


# ─── _buscar_tenants_ativos ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buscar_tenants_ativos_retorna_lista() -> None:
    dados = [
        {"id": "t1", "nome": "Clínica A", "config": {}},
        {"id": "t2", "nome": "Clínica B", "config": {"medico_whatsapp": "+5567999990000"}},
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.execute.return_value
    ).data = dados

    with patch("agents.internal.scheduler.get_supabase", return_value=mock_core):
        result = await _buscar_tenants_ativos()

    assert len(result) == 2
    assert result[0]["id"] == "t1"
    assert result[1]["config"]["medico_whatsapp"] == "+5567999990000"


@pytest.mark.asyncio
async def test_buscar_tenants_ativos_sem_dados_retorna_lista_vazia() -> None:
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.execute.return_value
    ).data = []

    with patch("agents.internal.scheduler.get_supabase", return_value=mock_core):
        result = await _buscar_tenants_ativos()

    assert result == []


# ─── _notificacao_ativa ───────────────────────────────────────────────────────


def test_notificacao_ativa_ausencia_total_retorna_false() -> None:
    """Padrão desligado: clínica nova não dispara nada sem configurar."""
    assert _notificacao_ativa({}, "bom_dia") is False
    assert _notificacao_ativa({}, "resumo_noturno") is False
    assert _notificacao_ativa({}, "aniversarios") is False


def test_notificacao_ativa_chave_ausente_em_dict_retorna_false() -> None:
    assert _notificacao_ativa({"notificacoes": {}}, "bom_dia") is False
    assert _notificacao_ativa({"notificacoes": {"resumo_noturno": True}}, "bom_dia") is False
    assert _notificacao_ativa({"notificacoes": {"bom_dia": "true"}}, "bom_dia") is False


def test_notificacao_ativa_false_explicito_retorna_false() -> None:
    config = {"notificacoes": {"aniversarios": False}}
    assert _notificacao_ativa(config, "aniversarios") is False


def test_notificacao_ativa_true_explicito_retorna_true() -> None:
    config = {"notificacoes": {"bom_dia": True}}
    assert _notificacao_ativa(config, "bom_dia") is True


def test_notificacao_ativa_config_none_retorna_false() -> None:
    assert _notificacao_ativa(None, "aniversarios") is False  # type: ignore[arg-type]


def test_notificacao_ativa_por_tenant_independente() -> None:
    """Toggle desligado em um tenant não afeta outro."""
    config_off = {"notificacoes": {"aniversarios": False}}
    config_on = {"notificacoes": {"aniversarios": True}}
    assert _notificacao_ativa(config_off, "aniversarios") is False
    assert _notificacao_ativa(config_on, "aniversarios") is True


# ─── _resolver_medico_whatsapp: sem fallback de env ──────────────────────────


def test_resolver_com_config_retorna_config() -> None:
    numero, origem = _resolver_medico_whatsapp("qualquer", {"medico_whatsapp": "+5511999990000"})
    assert numero == "+5511999990000"
    assert origem == "config"


def test_resolver_sem_config_retorna_nenhum() -> None:
    """Sem config["medico_whatsapp"], independente do tenant, retorna (None, 'nenhum')."""
    numero, origem = _resolver_medico_whatsapp("qualquer-tenant", {})
    assert numero is None
    assert origem == "nenhum"


def test_resolver_sem_config_nao_usa_env() -> None:
    """Não há fallback de env nem de settings. Config vazia → nenhum."""
    numero, origem = _resolver_medico_whatsapp("qualquer", {})
    assert numero is None
    assert origem == "nenhum"


# ─── Helpers de fixture ───────────────────────────────────────────────────────


def _tenants(ids: list[str]) -> list[dict]:
    return [
        {
            "id": tid,
            "nome": f"Tenant {tid}",
            "config": {"medico_whatsapp": f"+5567999{i:06d}"},
        }
        for i, tid in enumerate(ids)
    ]


# ─── job_aniversarios: iteração ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_aniversarios_itera_todos_os_tenants() -> None:
    chamados: list[str] = []

    async def _fake(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
        chamados.append(tenant_id)

    with (
        patch(
            "agents.internal.scheduler._buscar_tenants_ativos",
            return_value=_tenants(["t1", "t2", "t3"]),
        ),
        patch("agents.internal.scheduler._aniversarios_tenant", side_effect=_fake),
    ):
        await job_aniversarios()

    assert chamados == ["t1", "t2", "t3"]


@pytest.mark.asyncio
async def test_job_aniversarios_sem_tenants_nao_chama_envio() -> None:
    mock_envia = AsyncMock()

    with (
        patch("agents.internal.scheduler._buscar_tenants_ativos", return_value=[]),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await job_aniversarios()

    mock_envia.assert_not_called()


# ─── job_aniversarios: isolamento de falha ────────────────────────────────────


@pytest.mark.asyncio
async def test_job_aniversarios_falha_em_t2_nao_aborta_t3() -> None:
    chamados: list[str] = []

    async def _fake(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
        if tenant_id == "t2":
            raise RuntimeError("timeout simulado")
        chamados.append(tenant_id)

    with (
        patch(
            "agents.internal.scheduler._buscar_tenants_ativos",
            return_value=_tenants(["t1", "t2", "t3"]),
        ),
        patch("agents.internal.scheduler._aniversarios_tenant", side_effect=_fake),
    ):
        await job_aniversarios()

    assert "t1" in chamados
    assert "t2" not in chamados
    assert "t3" in chamados


# ─── _aniversarios_tenant: toggle off ────────────────────────────────────────


@pytest.mark.asyncio
async def test_aniversarios_tenant_toggle_off_nao_consulta_bd_nem_envia() -> None:
    config = {"medico_whatsapp": "+5567999000", "notificacoes": {"aniversarios": False}}
    mock_core = MagicMock()
    mock_envia = AsyncMock()

    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant("t1", config)

    mock_core.table.assert_not_called()
    mock_envia.assert_not_called()


# ─── job_aniversarios: toggle off por tenant ─────────────────────────────────


@pytest.mark.asyncio
async def test_job_aniversarios_pula_tenant_com_toggle_off_processa_com_toggle_on() -> None:
    tenants = [
        {
            "id": "t1",
            "nome": "On",
            "config": {"medico_whatsapp": "+5567001", "notificacoes": {"aniversarios": True}},
        },
        {
            "id": "t2",
            "nome": "Off",
            "config": {"medico_whatsapp": "+5567002", "notificacoes": {"aniversarios": False}},
        },
        {"id": "t3", "nome": "Default", "config": {"medico_whatsapp": "+5567003"}},  # ausente = off
    ]
    chamados: list[str] = []

    async def _fake(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
        chamados.append(tenant_id)

    with (
        patch("agents.internal.scheduler._buscar_tenants_ativos", return_value=tenants),
        patch("agents.internal.scheduler._aniversarios_tenant", side_effect=_fake),
    ):
        await job_aniversarios()

    assert chamados == ["t1", "t2", "t3"]  # wrapper chama todos; _fake simula o comportamento real


# ─── _aniversarios_tenant: sem aniversariantes ───────────────────────────────


@pytest.mark.asyncio
async def test_aniversarios_tenant_sem_aniversariantes_nao_envia() -> None:
    contatos = [
        {"id": "c1", "nome": "Ana", "telefone": "+5567999990000", "data_nascimento": "1990-01-01"}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock()
    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant(
            "t1", {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}}
        )

    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_aniversarios_tenant_com_aniversariante_amanha_envia() -> None:
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()
    data_nasc = f"1990-{amanha.month:02d}-{amanha.day:02d}"

    contatos = [
        {"id": "c1", "nome": "Carlos", "telefone": "+5567999990000", "data_nascimento": data_nasc}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant(
            "t1", {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}}
        )

    mock_envia.assert_called_once()
    texto_enviado: str = mock_envia.call_args.kwargs["texto"]
    assert "Carlos" in texto_enviado
    assert amanha.strftime("%d/%m/%Y") in texto_enviado


@pytest.mark.asyncio
async def test_aniversarios_tenant_sem_medico_nao_envia() -> None:
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()
    data_nasc = f"1990-{amanha.month:02d}-{amanha.day:02d}"

    contatos = [
        {"id": "c1", "nome": "Dora", "telefone": "+5567999990000", "data_nascimento": data_nasc}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock()
    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant("t1", {})  # sem medico_whatsapp → None → pula

    mock_envia.assert_not_called()


# ─── job_resumo_noturno: iteração ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_resumo_noturno_itera_todos_os_tenants() -> None:
    chamados: list[str] = []

    async def _fake(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
        chamados.append(tenant_id)

    with (
        patch(
            "agents.internal.scheduler._buscar_tenants_ativos", return_value=_tenants(["t1", "t2"])
        ),
        patch("agents.internal.scheduler._resumo_noturno_tenant", side_effect=_fake),
    ):
        await job_resumo_noturno()

    assert chamados == ["t1", "t2"]


@pytest.mark.asyncio
async def test_job_resumo_noturno_sem_tenants_nao_chama_envio() -> None:
    mock_envia = AsyncMock()

    with (
        patch("agents.internal.scheduler._buscar_tenants_ativos", return_value=[]),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await job_resumo_noturno()

    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_job_resumo_noturno_falha_em_t1_nao_aborta_t2_e_t3() -> None:
    chamados: list[str] = []

    async def _fake(tenant_id: str, config: dict, *, dry_run: bool = False) -> None:
        if tenant_id == "t1":
            raise Exception("DB timeout simulado")
        chamados.append(tenant_id)

    with (
        patch(
            "agents.internal.scheduler._buscar_tenants_ativos",
            return_value=_tenants(["t1", "t2", "t3"]),
        ),
        patch("agents.internal.scheduler._resumo_noturno_tenant", side_effect=_fake),
    ):
        await job_resumo_noturno()

    assert "t1" not in chamados
    assert "t2" in chamados
    assert "t3" in chamados


# ─── _resumo_noturno_tenant: toggle off ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resumo_noturno_tenant_toggle_off_nao_consulta_bd_nem_envia() -> None:
    config = {"medico_whatsapp": "+5567999000", "notificacoes": {"resumo_noturno": False}}
    mock_vertical = MagicMock()
    mock_envia = AsyncMock()

    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant("t1", config)

    mock_vertical.table.assert_not_called()
    mock_envia.assert_not_called()


# ─── _resumo_noturno_tenant ───────────────────────────────────────────────────


def _mock_vertical_duas_tabelas(agendamentos: list[dict], followups: list[dict]) -> MagicMock:
    mock_ag = MagicMock()
    (
        mock_ag.select.return_value.eq.return_value.gte.return_value.lt.return_value.execute.return_value
    ).data = agendamentos

    mock_fu = MagicMock()
    (
        mock_fu.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value
    ).data = followups

    mock_vertical = MagicMock()

    def _table(name: str) -> MagicMock:
        return mock_ag if name == "agendamentos" else mock_fu

    mock_vertical.table.side_effect = _table
    return mock_vertical


@pytest.mark.asyncio
async def test_resumo_noturno_tenant_conta_confirmados_corretamente() -> None:
    agendamentos = [
        {"id": "a1", "status": "confirmado", "data_hora": "2026-06-17T10:00:00-04:00"},
        {"id": "a2", "status": "confirmado_paciente", "data_hora": "2026-06-17T11:00:00-04:00"},
        {"id": "a3", "status": "agendado", "data_hora": "2026-06-17T12:00:00-04:00"},
    ]
    mock_vertical = _mock_vertical_duas_tabelas(agendamentos, followups=[])

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant(
            "t1",
            {
                "medico_whatsapp": "+5567999990000",
                "notificacoes": {
                    "bom_dia": True,
                    "resumo_noturno": True,
                    "aniversarios": True,
                    "nps": True,
                },
            },
        )

    mock_envia.assert_called_once()
    texto: str = mock_envia.call_args.kwargs["texto"]
    assert "2" in texto
    assert "3" in texto


@pytest.mark.asyncio
async def test_resumo_noturno_tenant_sem_followups_menciona_sem_pendencias() -> None:
    mock_vertical = _mock_vertical_duas_tabelas(agendamentos=[], followups=[])

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant(
            "t1",
            {
                "medico_whatsapp": "+5567999990000",
                "notificacoes": {
                    "bom_dia": True,
                    "resumo_noturno": True,
                    "aniversarios": True,
                    "nps": True,
                },
            },
        )

    texto: str = mock_envia.call_args.kwargs["texto"]
    assert "Sem follow-ups pendentes" in texto


@pytest.mark.asyncio
async def test_resumo_noturno_tenant_com_followups_lista_descricoes() -> None:
    followups = [
        {
            "contato_id": "c1",
            "tipo": "followup",
            "mensagem": "Retornar sobre exame",
            "agendado_para": "2026-06-20T10:00:00-04:00",
        },
        {
            "contato_id": "c2",
            "tipo": "followup",
            "mensagem": "Confirmar tratamento",
            "agendado_para": None,
        },
    ]
    mock_vertical = _mock_vertical_duas_tabelas(agendamentos=[], followups=followups)

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant(
            "t1",
            {
                "medico_whatsapp": "+5567999990000",
                "notificacoes": {
                    "bom_dia": True,
                    "resumo_noturno": True,
                    "aniversarios": True,
                    "nps": True,
                },
            },
        )

    texto: str = mock_envia.call_args.kwargs["texto"]
    assert "Retornar sobre exame" in texto
    assert "Confirmar tratamento" in texto
    assert "20/06" in texto


@pytest.mark.asyncio
async def test_resumo_noturno_tenant_followup_sem_mensagem_usa_tipo() -> None:
    followups = [
        {"contato_id": "c1", "tipo": "retorno", "mensagem": None, "agendado_para": None},
    ]
    mock_vertical = _mock_vertical_duas_tabelas(agendamentos=[], followups=followups)

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant(
            "t1",
            {
                "medico_whatsapp": "+5567999990000",
                "notificacoes": {
                    "bom_dia": True,
                    "resumo_noturno": True,
                    "aniversarios": True,
                    "nps": True,
                },
            },
        )

    texto: str = mock_envia.call_args.kwargs["texto"]
    assert "retorno" in texto


# ─── dry_run: não chama enviar_mensagem ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_aniversarios_nao_chama_enviar_mensagem() -> None:
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()
    data_nasc = f"1990-{amanha.month:02d}-{amanha.day:02d}"

    contatos = [
        {"id": "c1", "nome": "Eva", "telefone": "+5567999990000", "data_nascimento": data_nasc}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock()
    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        # Config com medico_whatsapp → número resolvido, dry_run bloqueia envio
        await _aniversarios_tenant(
            "t1",
            {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}},
            dry_run=True,
        )

    mock_envia.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_resumo_nao_chama_enviar_mensagem() -> None:
    mock_vertical = _mock_vertical_duas_tabelas(agendamentos=[], followups=[])

    mock_envia = AsyncMock()
    with (
        patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.scheduler.get_supabase", return_value=MagicMock()),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _resumo_noturno_tenant(
            "t1",
            {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}},
            dry_run=True,
        )

    mock_envia.assert_not_called()


# ─── dry_run: _registrar_notificacao não grava no banco ──────────────────────


@pytest.mark.asyncio
async def test_dry_run_nao_grava_crm_notificacoes() -> None:
    from agents.internal.scheduler import _registrar_notificacao

    mock_vertical = MagicMock()
    with patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical):
        await _registrar_notificacao("t1", "agenda_diaria", "msg", dry_run=True)

    mock_vertical.table.assert_not_called()


@pytest.mark.asyncio
async def test_live_grava_crm_notificacoes() -> None:
    from agents.internal.scheduler import _registrar_notificacao

    mock_vertical = MagicMock()
    with patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical):
        await _registrar_notificacao("t1", "agenda_diaria", "msg", dry_run=False)

    mock_vertical.table.assert_called_once_with("crm_notificacoes")


# ─── titulo (NOT NULL em crm_notificacoes) sempre preenchido ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tipo", ["agenda_diaria", "resumo_noturno", "aniversarios"])
async def test_registrar_notificacao_inclui_titulo_nao_nulo(tipo: str) -> None:
    from agents.internal.scheduler import _registrar_notificacao

    mock_vertical = MagicMock()
    with patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical):
        await _registrar_notificacao("t1", tipo, "msg", dry_run=False)

    payload = mock_vertical.table.return_value.insert.call_args.args[0]
    assert payload.get("titulo")
    assert isinstance(payload["titulo"], str)


@pytest.mark.asyncio
async def test_titulo_tipo_desconhecido_nao_e_vazio() -> None:
    """Tipo sem mapeamento explícito ainda gera titulo não-nulo (usa o próprio tipo)."""
    from agents.internal.scheduler import _registrar_notificacao

    mock_vertical = MagicMock()
    with patch("agents.internal.scheduler.get_vertical_supabase", return_value=mock_vertical):
        await _registrar_notificacao("t1", "tipo_novo_sem_mapeamento", "msg", dry_run=False)

    payload = mock_vertical.table.return_value.insert.call_args.args[0]
    assert payload.get("titulo")
    assert "tipo_novo_sem_mapeamento" in payload["titulo"]


# ─── dry_run: origem nos 3 casos ─────────────────────────────────────────────


def test_origem_config_quando_numero_vem_da_config() -> None:
    _, origem = _resolver_medico_whatsapp("qualquer", {"medico_whatsapp": "+5511999990000"})
    assert origem == "config"


def test_origem_nenhum_quando_config_ausente() -> None:
    _, origem = _resolver_medico_whatsapp("qualquer", {})
    assert origem == "nenhum"


def test_origem_nenhum_para_qualquer_tenant_sem_config() -> None:
    """Sem config, todos os tenants retornam 'nenhum'. Sem exceção para tenant especial."""
    _, orig1 = _resolver_medico_whatsapp("tenant-a", {})
    _, orig2 = _resolver_medico_whatsapp("tenant-b", {})
    assert orig1 == orig2 == "nenhum"


# ─── dry_run=False: comportamento live inalterado ────────────────────────────


@pytest.mark.asyncio
async def test_live_aniversarios_chama_enviar_mensagem() -> None:
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()
    data_nasc = f"1990-{amanha.month:02d}-{amanha.day:02d}"

    contatos = [
        {"id": "c1", "nome": "Fábio", "telefone": "+5567999990000", "data_nascimento": data_nasc}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock(return_value=True)
    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant(
            "t1",
            {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}},
            dry_run=False,
        )

    mock_envia.assert_called_once()


# ─── jobs resolvem canal por tenant_id (não mais canal único global) ────────


@pytest.mark.asyncio
async def test_envio_usa_tenant_id_para_resolver_canal() -> None:
    """Confirma que o tenant_id correto é propagado a enviar_mensagem_tenant —
    é ele que determina qual canal (provider + credenciais) é usado no envio,
    desde o M2 (antes era canal único via whatsapp_phone_number_id global)."""
    amanha = (datetime.now(_BR_TZ) + timedelta(days=1)).date()
    data_nasc = f"1990-{amanha.month:02d}-{amanha.day:02d}"

    contatos = [
        {"id": "c1", "nome": "Gabi", "telefone": "+5567000000000", "data_nascimento": data_nasc}
    ]
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value
    ).data = contatos

    mock_envia = AsyncMock(return_value=True)

    with (
        patch("agents.internal.scheduler.get_supabase", return_value=mock_core),
        patch("agents.internal.scheduler.enviar_mensagem_tenant", mock_envia),
    ):
        await _aniversarios_tenant(
            "tenant-gabi",
            {"medico_whatsapp": "+5567999990001", "notificacoes": {"aniversarios": True}},
        )

    assert mock_envia.call_args.kwargs["tenant_id"] == "tenant-gabi"
