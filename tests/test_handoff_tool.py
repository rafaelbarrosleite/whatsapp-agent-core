from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tools.verticals.clinica.handoff import (
    _MSG_SEM_ATENDENTE,
    _make_transferir_para_atendente,
    _status_lead,
)

_PATCH_GET_SUPABASE = "tools.verticals.clinica.handoff.get_supabase"
_PATCH_OBTER_CONVERSA = "tools.verticals.clinica.handoff.obter_ou_criar_conversa"
_PATCH_SELECIONAR = "tools.verticals.clinica.handoff.selecionar_proximo_atendente"
_PATCH_BUSCAR_HANDOFF_ATIVO = "tools.verticals.clinica.handoff.buscar_handoff_ativo"
_PATCH_REGISTRAR = "tools.verticals.clinica.handoff.registrar_handoff"
_PATCH_ENVIAR = "tools.verticals.clinica.handoff.enviar_mensagem_tenant"


def _mock_core_db_com_contato(nome: str | None = "João Silva") -> MagicMock:
    core_db = MagicMock()
    chain = core_db.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain.limit.return_value.execute.return_value.data = [
        {"nome": nome, "telefone": "+5511999990000"}
    ]
    return core_db


async def test_sem_atendente_disponivel_nao_pausa_agente_e_retorna_fallback():
    vertical_client = MagicMock()
    core_db = _mock_core_db_com_contato("João Silva")
    tool = _make_transferir_para_atendente(vertical_client)("tenant-1")

    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1"),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch(_PATCH_SELECIONAR, new=AsyncMock(return_value=None)),
        patch(_PATCH_REGISTRAR, new=AsyncMock()) as mock_registrar,
        patch(_PATCH_ENVIAR, new=AsyncMock()) as mock_enviar,
    ):
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "atendente",
                "motivo": "Quer agendar",
                "resumo": "Paciente quer agendar consulta.",
            }
        )

    assert resultado == _MSG_SEM_ATENDENTE
    mock_registrar.assert_not_called()
    mock_enviar.assert_not_called()
    assert core_db.table.return_value.update.call_count == 0


async def test_role_destino_invalido_retorna_erro_sem_tocar_banco():
    vertical_client = MagicMock()
    tool = _make_transferir_para_atendente(vertical_client)("tenant-1")

    with patch(_PATCH_GET_SUPABASE) as mock_get_supabase:
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "gerente",  # role do sistema antigo, não existe mais
                "motivo": "x",
                "resumo": "y",
            }
        )

    assert "Erro interno" in resultado
    mock_get_supabase.return_value.table.assert_not_called()


async def test_handoff_completo_com_atendente_disponivel():
    vertical_client = MagicMock()
    perfil = {"id": "perfil-1", "nome": "Maria", "role": "atendente", "telefone": "+5511988880000"}
    core_db = _mock_core_db_com_contato("João Silva")

    registrar_resultado = AsyncMock(return_value={"id": "handoff-1"})
    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_SELECIONAR, new=AsyncMock(return_value=perfil)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1") as mock_obter_conversa,
        patch(_PATCH_REGISTRAR, new=registrar_resultado) as mock_registrar,
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)) as mock_enviar,
    ):
        tool = _make_transferir_para_atendente(vertical_client)("tenant-1")
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "atendente",
                "motivo": "Quer agendar consulta",
                "resumo": "João, +5511999990000, quer agendar para semana que vem.",
            }
        )

    # Atendente = fila genérica: o texto ao paciente não cita o nome do perfil.
    assert "atendimento" in resultado.lower()
    mock_obter_conversa.assert_called_once_with(core_db, "tenant-1", "contato-1")
    mock_registrar.assert_called_once()
    assert mock_registrar.call_args.kwargs["perfil"] == perfil

    # Transferência para atendente NÃO pausa o agente (mudança de fluxo
    # 2026-07-12): a pausa acontece só quando alguém clica "Assumir" no CRM.
    assert core_db.table.return_value.update.call_count == 0

    # Notifica o WhatsApp pessoal do atendente, não o do paciente.
    mock_enviar.assert_called_once()
    assert mock_enviar.call_args.kwargs["numero_destino"] == "+5511988880000"


async def test_handoff_ativo_reutiliza_transferencia_sem_criar_novo_handoff():
    vertical_client = MagicMock()
    core_db = _mock_core_db_com_contato("João Silva")
    handoff_ativo = {
        "id": "handoff-1",
        "status": "aguardando",
        "perfil_nome": "Maria",
    }

    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1"),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=handoff_ativo)),
        patch(_PATCH_SELECIONAR, new=AsyncMock()) as mock_selecionar,
        patch(_PATCH_REGISTRAR, new=AsyncMock()) as mock_registrar,
        patch(_PATCH_ENVIAR, new=AsyncMock()) as mock_enviar,
    ):
        tool = _make_transferir_para_atendente(vertical_client)("tenant-1")
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "atendente",
                "motivo": "Quer agendar consulta particular",
                "resumo": "João quer agendar consulta particular.",
            }
        )

    assert "Maria" in resultado
    mock_selecionar.assert_not_called()
    mock_registrar.assert_not_called()
    mock_enviar.assert_not_called()


async def test_handoff_sem_telefone_atendente_pula_notificacao_sem_falhar():
    vertical_client = MagicMock()
    perfil = {"id": "perfil-1", "nome": "Maria", "role": "atendente", "telefone": None}
    core_db = _mock_core_db_com_contato("João Silva")

    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_SELECIONAR, new=AsyncMock(return_value=perfil)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1"),
        patch(_PATCH_REGISTRAR, new=AsyncMock(return_value={"id": "handoff-1"})),
        patch(_PATCH_ENVIAR, new=AsyncMock()) as mock_enviar,
    ):
        tool = _make_transferir_para_atendente(vertical_client)("tenant-1")
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "atendente",
                "motivo": "Quer agendar",
                "resumo": "resumo",
            }
        )

    assert "atendimento" in resultado.lower()
    mock_enviar.assert_not_called()


async def test_handoff_concierge_envia_whatsapp_com_link_wa_me_do_paciente():
    """Concierge (Laís) inicia a conversa pelo WhatsApp dela: o alerta vai para o
    número DELA e contém o link wa.me clicável do PACIENTE."""
    vertical_client = MagicMock()
    perfil = {"id": "perfil-c", "nome": "Laís", "role": "concierge", "telefone": "+5511977770000"}
    # _mock_core_db_com_contato usa telefone do paciente = +5511999990000
    core_db = _mock_core_db_com_contato("João Silva")

    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_SELECIONAR, new=AsyncMock(return_value=perfil)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1"),
        patch(_PATCH_REGISTRAR, new=AsyncMock(return_value={"id": "handoff-1"})),
        patch(
            "tools.verticals.clinica.handoff.atribuir_lead_por_contato",
            new=AsyncMock(return_value="lead-1"),
        ),
        patch("tools.verticals.clinica.handoff.notificar_handoff"),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)) as mock_enviar,
    ):
        tool = _make_transferir_para_atendente(vertical_client)("tenant-1")
        resultado = await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "concierge",
                "motivo": "Pós-operatório de joelho",
                "resumo": "João, pós-op, dor persistente.",
            }
        )

    assert "Laís" in resultado
    mock_enviar.assert_called_once()
    kwargs = mock_enviar.call_args.kwargs
    assert kwargs["numero_destino"] == "+5511977770000"  # WhatsApp da concierge
    assert "https://wa.me/5511999990000" in kwargs["texto"]  # link do paciente


# ─── status do lead (novo vs já existente) ───────────────────────────────────


def test_status_lead_none_e_novo():
    assert "novo" in _status_lead(None).lower()


def test_status_lead_criado_agora_e_novo():
    recente = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert "novo" in _status_lead({"created_at": recente}).lower()


def test_status_lead_antigo_e_ja_existente():
    antigo = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert "existente" in _status_lead({"created_at": antigo}).lower()


async def test_handoff_avisa_status_do_lead_no_whatsapp():
    """O alerta ao humano traz o status do lead (novo / já existente)."""
    vertical_client = MagicMock()
    perfil = {"id": "perfil-c", "nome": "Laís", "role": "concierge", "telefone": "+5511977770000"}
    core_db = _mock_core_db_com_contato("João Silva")

    with (
        patch(_PATCH_GET_SUPABASE, return_value=core_db),
        patch(_PATCH_SELECIONAR, new=AsyncMock(return_value=perfil)),
        patch(_PATCH_BUSCAR_HANDOFF_ATIVO, new=AsyncMock(return_value=None)),
        patch(_PATCH_OBTER_CONVERSA, return_value="conversa-1"),
        patch(_PATCH_REGISTRAR, new=AsyncMock(return_value={"id": "handoff-1"})),
        patch(
            "tools.verticals.clinica.handoff.buscar_lead_por_contato",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "tools.verticals.clinica.handoff.atribuir_lead_por_contato",
            new=AsyncMock(return_value="lead-1"),
        ),
        patch("tools.verticals.clinica.handoff.notificar_handoff"),
        patch(_PATCH_ENVIAR, new=AsyncMock(return_value=True)) as mock_enviar,
    ):
        tool = _make_transferir_para_atendente(vertical_client)("tenant-1")
        await tool.ainvoke(
            {
                "contato_id": "contato-1",
                "role_destino": "concierge",
                "motivo": "Pós-operatório",
                "resumo": "resumo",
            }
        )

    assert "novo" in mock_enviar.call_args.kwargs["texto"].lower()
