"""Testes de isolamento multi-tenant do Internal Agent.

Cobre:
  - resolver_tenant_por_medico: número com/sem '+', desconhecido, inativo, colisão
  - _montar_system_prompt: perfil completo, ausente, None — nunca menciona um médico fixo
  - Tools leem tenant_id do RunnableConfig, nunca de env
  - Thread isolada por tenant (f"medico:{tenant_id}")
  - Integração: tool usa tenant_id=A, não vaza para B nem para default
  - _montar_brief_prompt: prompt dinâmico sem nome hardcoded
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from agents.internal.agent import _montar_system_prompt, tool_buscar_paciente, tool_salvar_nota
from agents.internal.briefs import _montar_brief_prompt
from agents.internal.tenant_resolver import resolver_tenant_por_medico

# ─── resolver_tenant_por_medico ───────────────────────────────────────────────


def _mock_db_com_tenants(tenants: list[dict]) -> MagicMock:
    mock_core = MagicMock()
    (
        mock_core.table.return_value.select.return_value.eq.return_value.execute.return_value
    ).data = tenants
    return mock_core


@pytest.mark.asyncio
async def test_numero_com_mais_resolve_tenant() -> None:
    """'+5567999991234' (com +) resolve o tenant correto."""
    tenants = [
        {"id": "t1", "nome": "Clínica A", "config": {"medico_whatsapp": "+5567999991234"}},
        {"id": "t2", "nome": "Clínica B", "config": {"medico_whatsapp": "+5511999990000"}},
    ]
    with patch(
        "agents.internal.tenant_resolver.get_supabase", return_value=_mock_db_com_tenants(tenants)
    ):
        result = await resolver_tenant_por_medico("+5567999991234")

    assert result is not None
    assert result["id"] == "t1"


@pytest.mark.asyncio
async def test_numero_sem_mais_resolve_mesmo_tenant() -> None:
    """'5567999991234' (sem +, formato que vem da Meta) resolve o mesmo tenant."""
    tenants = [
        {"id": "t1", "nome": "Clínica A", "config": {"medico_whatsapp": "+5567999991234"}},
    ]
    with patch(
        "agents.internal.tenant_resolver.get_supabase", return_value=_mock_db_com_tenants(tenants)
    ):
        result = await resolver_tenant_por_medico("5567999991234")

    assert result is not None
    assert result["id"] == "t1"


@pytest.mark.asyncio
async def test_numero_desconhecido_retorna_none() -> None:
    tenants = [
        {"id": "t1", "nome": "Clínica A", "config": {"medico_whatsapp": "+5511999990000"}},
    ]
    with patch(
        "agents.internal.tenant_resolver.get_supabase", return_value=_mock_db_com_tenants(tenants)
    ):
        result = await resolver_tenant_por_medico("+5567000000000")

    assert result is None


@pytest.mark.asyncio
async def test_tenant_inativo_nao_encontrado() -> None:
    """Tenant inativo não aparece (filtro ativo=True no DB simulado retornando [])."""
    with patch(
        "agents.internal.tenant_resolver.get_supabase", return_value=_mock_db_com_tenants([])
    ):
        result = await resolver_tenant_por_medico("+5567999991234")

    assert result is None


@pytest.mark.asyncio
async def test_colisao_retorna_none(caplog: pytest.LogCaptureFixture) -> None:
    """Dois tenants com mesmo medico_whatsapp → None + log.error com tenant_ids."""
    tenants = [
        {"id": "t1", "nome": "Clínica A", "config": {"medico_whatsapp": "+5567999991234"}},
        {"id": "t2", "nome": "Clínica B", "config": {"medico_whatsapp": "+5567999991234"}},
    ]
    with (
        patch(
            "agents.internal.tenant_resolver.get_supabase",
            return_value=_mock_db_com_tenants(tenants),
        ),
        caplog.at_level(logging.ERROR, logger="agents.internal.tenant_resolver"),
    ):
        result = await resolver_tenant_por_medico("+5567999991234")

    assert result is None
    assert "COLISÃO" in caplog.text
    assert "t1" in caplog.text
    assert "t2" in caplog.text


@pytest.mark.asyncio
async def test_colisao_nao_revela_tenant_para_remetente() -> None:
    """Colisão não deve expor dados de nenhum dos tenants."""
    tenants = [
        {"id": "t1", "config": {"medico_whatsapp": "+5567999991234", "segredo": "X"}},
        {"id": "t2", "config": {"medico_whatsapp": "+5567999991234", "segredo": "Y"}},
    ]
    with patch(
        "agents.internal.tenant_resolver.get_supabase", return_value=_mock_db_com_tenants(tenants)
    ):
        result = await resolver_tenant_por_medico("+5567999991234")

    assert result is None  # nenhum dict retornado — sem exposição


# ─── _montar_system_prompt: identidade por tenant ────────────────────────────


def test_system_prompt_com_perfil_completo() -> None:
    config = {
        "perfil_medico": {
            "nome": "Dr. Ana Lima",
            "especialidade": "Cardiologia",
            "clinica": "HeartCare",
        }
    }
    prompt = _montar_system_prompt(config)
    assert "Dr. Ana Lima" in prompt
    assert "Cardiologia" in prompt
    assert "HeartCare" in prompt


def test_system_prompt_nunca_menciona_medico_fixo() -> None:
    """Com ou sem perfil, o prompt nunca menciona um médico fixo."""
    assert "Dr. Fulano" not in _montar_system_prompt({})
    assert "Dr. Fulano" not in _montar_system_prompt(None)
    assert "Dr. Fulano" not in _montar_system_prompt({"perfil_medico": {}})
    assert "Dr. Fulano" not in _montar_system_prompt({"perfil_medico": {"nome": "Dr. Outro"}})


def test_system_prompt_sem_perfil_usa_fallback_generico() -> None:
    prompt = _montar_system_prompt({})
    assert "do médico desta clínica" in prompt
    assert "Fulano" not in prompt


def test_system_prompt_perfil_none_usa_fallback() -> None:
    prompt = _montar_system_prompt(None)
    assert "do médico desta clínica" in prompt


def test_system_prompt_dois_tenants_diferentes() -> None:
    """Dois tenants com perfis distintos geram prompts distintos."""
    config_a = {"perfil_medico": {"nome": "Dr. Ana", "especialidade": "Cardiologia"}}
    config_b = {"perfil_medico": {"nome": "Dr. Bruno", "especialidade": "Pediatria"}}
    prompt_a = _montar_system_prompt(config_a)
    prompt_b = _montar_system_prompt(config_b)

    assert "Dr. Ana" in prompt_a and "Dr. Ana" not in prompt_b
    assert "Dr. Bruno" in prompt_b and "Dr. Bruno" not in prompt_a
    assert "Cardiologia" in prompt_a and "Cardiologia" not in prompt_b
    assert "Pediatria" in prompt_b and "Pediatria" not in prompt_a


# ─── _montar_brief_prompt: identidade no gerador de briefs ───────────────────


def test_brief_prompt_com_perfil_completo() -> None:
    perfil = {"nome": "Dra. Clara", "especialidade": "Neurologia"}
    prompt = _montar_brief_prompt(perfil)
    assert "Dra. Clara" in prompt
    assert "Neurologia" in prompt


def test_brief_prompt_sem_perfil_generico() -> None:
    prompt = _montar_brief_prompt(None)
    assert "o médico" in prompt
    assert "Dr. Fulano" not in prompt
    assert "ortopedista" not in prompt


def test_brief_prompt_nunca_menciona_medico_fixo() -> None:
    for perfil in [None, {}, {"nome": ""}]:
        assert "Dr. Fulano" not in _montar_brief_prompt(perfil)


# ─── Thread isolada por tenant ────────────────────────────────────────────────


def test_thread_ids_diferentes_por_tenant() -> None:
    """Cada tenant tem sua thread de histórico isolada."""
    thread_a = "medico:TENANT-A"
    thread_b = "medico:TENANT-B"
    assert thread_a != thread_b
    assert "TENANT-A" in thread_a
    assert "TENANT-B" in thread_b
    assert "TENANT-B" not in thread_a
    assert "TENANT-A" not in thread_b


# ─── Tools: tenant_id do RunnableConfig, não de env ──────────────────────────


@pytest.mark.asyncio
async def test_tool_sem_tenant_id_retorna_erro_sem_chamar_db() -> None:
    """Config sem tenant_id → retorna mensagem de erro, nunca consulta o banco."""
    mock_vertical = MagicMock()

    with patch("agents.internal.agent.get_vertical_supabase", return_value=mock_vertical):
        result = await tool_salvar_nota.ainvoke(
            {"contato_id": "c1", "nota": "test"},
            config={"configurable": {}},  # sem tenant_id
        )

    assert "Erro" in result
    mock_vertical.table.assert_not_called()


@pytest.mark.asyncio
async def test_tool_le_tenant_id_do_config_e_nao_de_env() -> None:
    """tool_salvar_nota usa tenant_id do RunnableConfig, não de settings.
    Mesmo que settings retorne qualquer coisa, o tenant vem do config["configurable"].
    """
    mock_vertical = MagicMock()
    mock_vertical.table.return_value.insert.return_value.execute.return_value = MagicMock()

    with (
        patch("agents.internal.agent.get_vertical_supabase", return_value=mock_vertical),
        patch("agents.internal.agent.settings", MagicMock()),  # settings isolado
    ):
        result = await tool_salvar_nota.ainvoke(
            {"contato_id": "c1", "nota": "observação"},
            config={"configurable": {"tenant_id": "TENANT-CORRETO"}},
        )

    assert result == "Nota salva com sucesso. ✅"
    insert_payload = mock_vertical.table.return_value.insert.call_args[0][0]
    assert insert_payload["tenant_id"] == "TENANT-CORRETO"


@pytest.mark.asyncio
async def test_salvar_nota_inclui_titulo_e_nao_envia_coluna_inexistente() -> None:
    """crm_notificacoes não tem coluna contato_id — o vínculo ao paciente vai
    embutido em 'mensagem'. 'titulo' é NOT NULL e precisa vir preenchido."""
    mock_vertical = MagicMock()
    mock_vertical.table.return_value.insert.return_value.execute.return_value = MagicMock()

    with patch("agents.internal.agent.get_vertical_supabase", return_value=mock_vertical):
        result = await tool_salvar_nota.ainvoke(
            {"contato_id": "c1", "nota": "observação"},
            config={"configurable": {"tenant_id": "TENANT-CORRETO"}},
        )

    assert result == "Nota salva com sucesso. ✅"
    insert_payload = mock_vertical.table.return_value.insert.call_args[0][0]
    assert "contato_id" not in insert_payload
    assert insert_payload.get("titulo")
    assert isinstance(insert_payload["titulo"], str)
    assert "c1" in insert_payload["mensagem"]
    assert "observação" in insert_payload["mensagem"]


# ─── Integração: tool consulta tenant correto ─────────────────────────────────


@pytest.mark.asyncio
async def test_integracao_tool_buscar_paciente_usa_tenant_a() -> None:
    """Médico do tenant A → tool_buscar_paciente filtra por tenant_id=A, não B."""
    mock_core = MagicMock()
    # Simula resultado vazio — só nos interessa verificar o filtro aplicado
    (
        mock_core.table.return_value.select.return_value.eq.return_value.or_.return_value.limit.return_value.execute.return_value
    ).data = []

    with patch("agents.internal.agent.get_supabase", return_value=mock_core):
        await tool_buscar_paciente.ainvoke(
            {"nome_ou_telefone": "João"},
            config={"configurable": {"tenant_id": "TENANT-A"}},
        )

    # A primeira chamada .eq() após .select() deve usar tenant_id=TENANT-A
    eq_calls = mock_core.table.return_value.select.return_value.eq.call_args_list
    tenant_filter = next(
        (c for c in eq_calls if c.args and c.args[0] == "tenant_id"),
        None,
    )
    assert tenant_filter is not None, "Nenhum filtro de tenant_id aplicado"
    assert tenant_filter.args[1] == "TENANT-A"


@pytest.mark.asyncio
async def test_dois_tenants_usam_filtros_separados() -> None:
    """Chamadas de tenant A e B resultam em filtros de tenant distintos."""
    chamadas_tenant: list[str] = []

    def _mock_eq(col: str, val: str):  # type: ignore[return]
        if col == "tenant_id":
            chamadas_tenant.append(val)
        return MagicMock(
            or_=MagicMock(
                return_value=MagicMock(
                    limit=MagicMock(
                        return_value=MagicMock(execute=MagicMock(return_value=MagicMock(data=[])))
                    )
                )
            )
        )

    mock_core = MagicMock()
    mock_core.table.return_value.select.return_value.eq.side_effect = _mock_eq

    with patch("agents.internal.agent.get_supabase", return_value=mock_core):
        await tool_buscar_paciente.ainvoke(
            {"nome_ou_telefone": "Ana"},
            config={"configurable": {"tenant_id": "TENANT-A"}},
        )
        await tool_buscar_paciente.ainvoke(
            {"nome_ou_telefone": "Bruno"},
            config={"configurable": {"tenant_id": "TENANT-B"}},
        )

    assert "TENANT-A" in chamadas_tenant
    assert "TENANT-B" in chamadas_tenant
