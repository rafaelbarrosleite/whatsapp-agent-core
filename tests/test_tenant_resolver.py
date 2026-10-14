from unittest.mock import MagicMock

import pytest

from gateway.tenant_resolver import TenantInfo, TenantNaoEncontradoError, TenantResolver

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _mock_client(rows: list[dict]) -> MagicMock:
    """Monta o chain do cliente Supabase para a query usada em TenantResolver.resolver().

    Chain: .table().select().eq().eq().limit().execute().data
    """
    client = MagicMock()
    (
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return client


# ---------------------------------------------------------------------------
# Casos principais
# ---------------------------------------------------------------------------


def test_numero_conhecido_retorna_tenant_info() -> None:
    client = _mock_client(
        [
            {
                "id": "canal-uuid",
                "tenant_id": "t-123",
                "provider": "cloud_api",
                "credenciais_ref": None,
                "tenants": {"vertical": "clinica"},
            }
        ]
    )
    resolver = TenantResolver(client=client)

    info = resolver.resolver("+5511888880000")

    assert info == TenantInfo(tenant_id="t-123", vertical="clinica", canal_id="canal-uuid")


def test_numero_desconhecido_levanta_erro() -> None:
    client = _mock_client([])
    resolver = TenantResolver(client=client)

    with pytest.raises(TenantNaoEncontradoError) as exc_info:
        resolver.resolver("+5500000000000")

    assert "+5500000000000" in str(exc_info.value)


def test_numero_armazenado_no_atributo_da_excecao() -> None:
    """O número deve estar acessível em exc.numero para logging estruturado."""
    client = _mock_client([])
    resolver = TenantResolver(client=client)

    with pytest.raises(TenantNaoEncontradoError) as exc_info:
        resolver.resolver("+5599999999999")

    assert exc_info.value.numero == "+5599999999999"


def test_canal_inativo_nao_retorna_tenant() -> None:
    """Canal com ativo=False não deve ser resolvido.

    O filtro .eq('ativo', True) na query exclui canais inativos — o mock
    simula isso retornando lista vazia, assim como o banco retornaria.
    """
    client = _mock_client([])
    resolver = TenantResolver(client=client)

    with pytest.raises(TenantNaoEncontradoError):
        resolver.resolver("+5511888880000")


def test_tenant_info_e_imutavel() -> None:
    """TenantInfo é frozen dataclass — atribuição deve falhar."""
    info = TenantInfo(tenant_id="t-1", vertical="garagem", canal_id="canal-1")

    with pytest.raises(Exception):  # FrozenInstanceError ou AttributeError
        info.tenant_id = "outro"  # type: ignore[misc]


def test_diferentes_verticais_sao_retornados_corretamente() -> None:
    for vertical in ("clinica", "garagem", "farmacia"):
        client = _mock_client(
            [
                {
                    "id": "canal-x",
                    "tenant_id": "t-x",
                    "provider": "cloud_api",
                    "credenciais_ref": None,
                    "tenants": {"vertical": vertical},
                }
            ]
        )
        resolver = TenantResolver(client=client)
        info = resolver.resolver("+5511000000000")
        assert info.vertical == vertical


def test_canal_id_retornado_corretamente() -> None:
    client = _mock_client(
        [
            {
                "id": "canal-abc",
                "tenant_id": "t-1",
                "provider": "cloud_api",
                "credenciais_ref": None,
                "tenants": {"vertical": "clinica"},
            }
        ]
    )
    resolver = TenantResolver(client=client)
    info = resolver.resolver("+5511999990000")
    assert info.canal_id == "canal-abc"


def test_provider_e_credenciais_ref_sao_retornados() -> None:
    client = _mock_client(
        [
            {
                "id": "canal-1",
                "tenant_id": "t-1",
                "provider": "cloud_api",
                "credenciais_ref": "WHATSAPP_CLINICA_X",
                "tenants": {"vertical": "clinica"},
            }
        ]
    )
    resolver = TenantResolver(client=client)
    info = resolver.resolver("+556733334444")
    assert info.provider == "cloud_api"
    assert info.credenciais_ref == "WHATSAPP_CLINICA_X"


def test_credenciais_ref_vazio_no_banco_vira_none() -> None:
    """credenciais_ref é TEXT NOT NULL no banco — string vazia também deve
    cair no fallback de settings, não ser tratada como referência válida."""
    client = _mock_client(
        [
            {
                "id": "canal-x",
                "tenant_id": "t-x",
                "provider": "cloud_api",
                "credenciais_ref": "",
                "tenants": {"vertical": "clinica"},
            }
        ]
    )
    resolver = TenantResolver(client=client)
    info = resolver.resolver("+5511000000000")
    assert info.credenciais_ref is None
