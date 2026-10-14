from unittest.mock import MagicMock

from langchain_core.tools import tool

from core.registry import ToolFactory, ToolRegistry
from tools.crm.contatos import registrar as registrar_crm


def _fake_factory(nome: str) -> ToolFactory:
    """Cria uma factory mínima para testes — não faz I/O."""

    def factory(tenant_id: str):
        @tool
        def fake(x: str) -> str:
            """Fake tool para testes."""
            return f"{nome}:{tenant_id}:{x}"

        fake.name = nome
        return fake

    return factory


def test_registry_vazio_retorna_lista_vazia():
    reg = ToolRegistry()
    assert reg.get_tools("clinica", "t1") == []


def test_tools_genericas_aparecem_em_qualquer_vertical():
    reg = ToolRegistry()
    reg.register_generic(_fake_factory("generica"))

    assert len(reg.get_tools("clinica", "t1")) == 1
    assert len(reg.get_tools("garagem", "t1")) == 1
    assert len(reg.get_tools("farmacia", "t1")) == 1


def test_tool_vertical_so_aparece_no_vertical_correto():
    reg = ToolRegistry()
    reg.register_generic(_fake_factory("generica"))
    reg.register_vertical("clinica", _fake_factory("agendamento"))

    clinica = reg.get_tools("clinica", "t1")
    garagem = reg.get_tools("garagem", "t1")

    assert len(clinica) == 2
    assert len(garagem) == 1
    assert {t.name for t in clinica} == {"generica", "agendamento"}


def test_vertical_desconhecido_retorna_so_genericas():
    reg = ToolRegistry()
    reg.register_generic(_fake_factory("generica"))
    reg.register_vertical("clinica", _fake_factory("agendamento"))

    tools = reg.get_tools("farmacia", "t1")  # vertical sem tools registradas
    assert len(tools) == 1
    assert tools[0].name == "generica"


def test_registered_verticals_lista_apenas_verticais_com_tools():
    reg = ToolRegistry()
    reg.register_vertical("clinica", _fake_factory("a"))
    reg.register_vertical("garagem", _fake_factory("b"))

    assert set(reg.registered_verticals()) == {"clinica", "garagem"}


def test_registrar_crm_adiciona_duas_tools_genericas():
    reg = ToolRegistry()
    mock_client = MagicMock()
    registrar_crm(reg, client=mock_client)

    tools = reg.get_tools("qualquer_vertical", "tenant-1")
    assert {t.name for t in tools} == {"buscar_contato", "criar_contato"}


def test_tenant_ids_distintos_geram_closures_distintas():
    """Cada get_tools com tenant_id diferente produz tools com contextos isolados."""
    reg = ToolRegistry()
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

    registrar_crm(reg, client=mock_client)

    [buscar_a] = [t for t in reg.get_tools("clinica", "tenant-A") if t.name == "buscar_contato"]
    [buscar_b] = [t for t in reg.get_tools("clinica", "tenant-B") if t.name == "buscar_contato"]

    buscar_a.invoke({"telefone": "+5511111111111"})
    buscar_b.invoke({"telefone": "+5511111111111"})

    eq_calls = mock_client.table.return_value.select.return_value.eq.call_args_list
    tenant_ids_usados = [c[0][1] for c in eq_calls if c[0][0] == "tenant_id"]
    assert "tenant-A" in tenant_ids_usados
    assert "tenant-B" in tenant_ids_usados
