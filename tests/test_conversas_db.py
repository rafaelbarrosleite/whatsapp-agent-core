from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import db.conversas as dbc
from db.conversas import (
    agente_ativo,
    obter_ou_criar_conversa,
    resolver_contato_id,
    resolver_conversa_para_entrada,
    salvar_mensagem,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_contatos(rows: list[dict]) -> MagicMock:
    """Mock do chain: .select('id').eq().eq().limit().execute().data"""
    m = MagicMock()
    (
        m.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return m


def _mock_conversas_select(rows: list[dict]) -> MagicMock:
    """Mock do chain .select().eq().eq().in_().order().limit().execute().data."""
    m = MagicMock()
    (
        m.select.return_value.eq.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value.data
    ) = rows
    return m


def _client_with_tables(**mocks: MagicMock) -> MagicMock:
    """Client cujo .table(name) retorna mocks distintos por nome de tabela."""
    client = MagicMock()
    default = MagicMock()
    client.table.side_effect = lambda name: mocks.get(name, default)
    return client


def _client_rpc(return_value: str) -> MagicMock:
    """Client com .rpc() configurado para retornar um UUID de conversa."""
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = return_value
    return client


# ---------------------------------------------------------------------------
# resolver_contato_id
# ---------------------------------------------------------------------------


def test_resolver_contato_id_retorna_id_quando_encontrado() -> None:
    m = _mock_contatos([{"id": "contato-1"}])
    client = _client_with_tables(contatos=m)
    assert resolver_contato_id(client, "t-1", "+5567999990000") == "contato-1"


def test_resolver_contato_id_retorna_none_quando_nao_encontrado() -> None:
    m = _mock_contatos([])
    client = _client_with_tables(contatos=m)
    assert resolver_contato_id(client, "t-1", "+5567000000000") is None


def test_resolver_contato_id_filtra_tenant_primeiro() -> None:
    """O primeiro .eq() deve ser tenant_id — nunca filtrar só por telefone."""
    m = _mock_contatos([])
    client = _client_with_tables(contatos=m)
    resolver_contato_id(client, "tenant-abc", "+5511999990000")
    primeiro_filtro = m.select.return_value.eq.call_args[0]
    assert primeiro_filtro == ("tenant_id", "tenant-abc")


# ---------------------------------------------------------------------------
# obter_ou_criar_conversa
# A lógica de SELECT / INSERT / updated_at vive na função PL/pgSQL do banco
# (migration 20240620000000). Os testes aqui verificam que o Python delega
# corretamente via client.rpc() com os parâmetros certos.
# ---------------------------------------------------------------------------


def test_obter_ou_criar_conversa_chama_rpc_com_params_corretos() -> None:
    client = _client_rpc("conv-uuid-1")
    result = obter_ou_criar_conversa(client, "tenant-1", "contato-1", "canal-1")
    assert result == "conv-uuid-1"
    client.rpc.assert_called_once_with(
        "obter_ou_criar_conversa",
        {"p_tenant_id": "tenant-1", "p_contato_id": "contato-1", "p_canal_id": "canal-1"},
    )


def test_obter_ou_criar_conversa_sem_canal_passa_none() -> None:
    client = _client_rpc("conv-uuid-2")
    obter_ou_criar_conversa(client, "tenant-1", "contato-1")
    params = client.rpc.call_args[0][1]
    assert params["p_canal_id"] is None


def test_obter_ou_criar_conversa_retorna_id_da_rpc() -> None:
    client = _client_rpc("uuid-esperado")
    assert obter_ou_criar_conversa(client, "t1", "c1") == "uuid-esperado"


# ---------------------------------------------------------------------------
# salvar_mensagem
# ---------------------------------------------------------------------------


def test_salvar_mensagem_paciente_persiste_papel_usuario() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    salvar_mensagem(client, "t-1", "conv-1", "paciente", "olá")
    payload = m.insert.call_args[0][0]
    assert payload["papel"] == "usuario"


def test_salvar_mensagem_agente_persiste_papel_agente() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    salvar_mensagem(client, "t-1", "conv-1", "agente", "como posso ajudar?")
    payload = m.insert.call_args[0][0]
    assert payload["papel"] == "agente"


def test_salvar_mensagem_inclui_conteudo() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    salvar_mensagem(client, "t-1", "conv-1", "paciente", "quero agendar")
    payload = m.insert.call_args[0][0]
    assert payload["conteudo"] == "quero agendar"


def test_salvar_mensagem_inclui_metadata_informado() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    meta = {"whatsapp_message_id": "wamid.abc123"}
    salvar_mensagem(client, "t-1", "conv-1", "paciente", "oi", meta)
    payload = m.insert.call_args[0][0]
    assert payload["metadata"] == meta


def test_salvar_mensagem_metadata_vazio_por_padrao() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    salvar_mensagem(client, "t-1", "conv-1", "agente", "tudo bem")
    payload = m.insert.call_args[0][0]
    assert payload["metadata"] == {}


def test_salvar_mensagem_inclui_tenant_id_e_conversa_id() -> None:
    m = MagicMock()
    client = _client_with_tables(mensagens=m)
    salvar_mensagem(client, "tenant-x", "conv-y", "agente", "ok")
    payload = m.insert.call_args[0][0]
    assert payload["tenant_id"] == "tenant-x"
    assert payload["conversa_id"] == "conv-y"


# ---------------------------------------------------------------------------
# agente_ativo
# ---------------------------------------------------------------------------


def test_agente_ativo_sem_contato_retorna_true() -> None:
    """Novo contato (nunca falou antes) → agente deve responder."""
    m_contatos = _mock_contatos([])
    client = _client_with_tables(contatos=m_contatos)
    assert agente_ativo(client, "t-1", "+5567999990000") is True


def test_agente_ativo_sem_conversa_retorna_true() -> None:
    """Contato existe mas sem conversa aberta → agente deve responder."""
    m_contatos = _mock_contatos([{"id": "contato-1"}])
    m_conversas = _mock_conversas_select([])
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    assert agente_ativo(client, "t-1", "+5567999990000") is True


def test_agente_ativo_aberta_com_flag_true_retorna_true() -> None:
    """Caso extra: conversa 'aberta' com agente_ativo=True → agente responde."""
    m_contatos = _mock_contatos([{"id": "contato-1"}])
    m_conversas = _mock_conversas_select([{"estado": "aberta", "agente_ativo": True}])
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    assert agente_ativo(client, "t-1", "+5567999990000") is True


def test_agente_ativo_aberta_com_flag_false_retorna_false() -> None:
    """Conversa 'aberta' mas agente_ativo=False (humano pausou o agente) → não responde."""
    m_contatos = _mock_contatos([{"id": "contato-1"}])
    m_conversas = _mock_conversas_select([{"estado": "aberta", "agente_ativo": False}])
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    assert agente_ativo(client, "t-1", "+5567999990000") is False


def test_agente_ativo_aguardando_humano_bloqueia_bia() -> None:
    """Cenário principal do fix: conversa em 'aguardando_humano' → humano no
    controle → o agente NÃO responde (antes retornava True por ignorar o estado)."""
    m_contatos = _mock_contatos([{"id": "contato-1"}])
    m_conversas = _mock_conversas_select([{"estado": "aguardando_humano", "agente_ativo": False}])
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    assert agente_ativo(client, "t-1", "+5567999990000") is False


def test_agente_ativo_aguardando_humano_com_flag_dessincronizada_bloqueia() -> None:
    """Defesa: mesmo que agente_ativo esteja True numa conversa
    'aguardando_humano' (linha dessincronizada), o estado manda → agente não responde."""
    m_contatos = _mock_contatos([{"id": "contato-1"}])
    m_conversas = _mock_conversas_select([{"estado": "aguardando_humano", "agente_ativo": True}])
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    assert agente_ativo(client, "t-1", "+5567999990000") is False


def test_agente_ativo_nao_consulta_conversas_para_contato_inexistente() -> None:
    """Se contato não existe, não deve nem consultar a tabela conversas."""
    m_contatos = _mock_contatos([])
    m_conversas = MagicMock()
    client = _client_with_tables(contatos=m_contatos, conversas=m_conversas)
    agente_ativo(client, "t-1", "+5567000000000")
    m_conversas.select.assert_not_called()


# ---------------------------------------------------------------------------
# resolver_conversa_para_entrada — entrada NUNCA reativa aguardando_humano
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _conv_ativa(estado: str, atividade_ha: timedelta, agente_ativo: bool = False) -> dict:
    return {
        "id": "conv-1",
        "estado": estado,
        "agente_ativo": agente_ativo,
        "updated_at": (_T0 - atividade_ha).isoformat(),
    }


def test_resolver_sem_conversa_ativa_cria_nova() -> None:
    client = MagicMock()
    with (
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=None),
        patch.object(dbc, "obter_ou_criar_conversa", return_value="conv-nova") as criar,
    ):
        assert resolver_conversa_para_entrada(client, "t1", "c1", "canal-1") == (
            "conv-nova",
            True,
            "nova",
        )
    criar.assert_called_once_with(client, "t1", "c1", "canal-1")


def test_resolver_conversa_aberta_reutiliza_mantendo_flag() -> None:
    client = MagicMock()
    conv = _conv_ativa("aberta", timedelta(minutes=5), agente_ativo=True)
    with (
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=conv),
        patch.object(dbc, "obter_ou_criar_conversa") as criar,
    ):
        assert resolver_conversa_para_entrada(client, "t1", "c1") == (
            "conv-1",
            True,
            "reuso_aberta",
        )
    criar.assert_not_called()


def test_resolver_aguardando_humano_recente_mantem_com_humano() -> None:
    """aguardando_humano → mantém com o humano; agente não responde (agente=False)."""
    client = MagicMock()
    conv = _conv_ativa("aguardando_humano", timedelta(minutes=30))
    with (
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=conv),
        patch.object(dbc, "_marcar_atividade") as marcar,
        patch.object(dbc, "obter_ou_criar_conversa") as criar,
    ):
        assert resolver_conversa_para_entrada(client, "t1", "c1") == (
            "conv-1",
            False,
            "reuso_mantem_humano",
        )
    marcar.assert_called_once_with(client, "t1", "conv-1")
    criar.assert_not_called()


def test_resolver_aguardando_humano_sem_resposta_humana_nao_reativa_bia() -> None:
    """Reprodução do bug: atendente assumiu mas ainda NÃO respondeu; paciente
    manda mensagem. O agente NÃO pode reativar — mantém com o humano (agente=False)."""
    client = MagicMock()
    conv = _conv_ativa("aguardando_humano", timedelta(hours=3))
    with (
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=conv),
        patch.object(dbc, "_marcar_atividade") as marcar,
        patch.object(dbc, "obter_ou_criar_conversa") as criar,
    ):
        assert resolver_conversa_para_entrada(client, "t1", "c1") == (
            "conv-1",
            False,
            "reuso_mantem_humano",
        )
    marcar.assert_called_once_with(client, "t1", "conv-1")
    criar.assert_not_called()


def test_resolver_aguardando_humano_antigo_nao_encerra_nem_cria_nova() -> None:
    """Mesmo >24h em aguardando_humano, a entrada NÃO encerra nem cria nova
    conversa — estado só muda por assumir/devolver explícitos."""
    client = MagicMock()
    conv = _conv_ativa("aguardando_humano", timedelta(hours=48))
    with (
        patch.object(dbc, "_buscar_conversa_ativa_row", return_value=conv),
        patch.object(dbc, "_marcar_atividade"),
        patch.object(dbc, "obter_ou_criar_conversa") as criar,
    ):
        assert resolver_conversa_para_entrada(client, "t1", "c1") == (
            "conv-1",
            False,
            "reuso_mantem_humano",
        )
    criar.assert_not_called()
