from unittest.mock import MagicMock

from db.handoff import (
    assumir_handoff,
    buscar_handoff_ativo,
    devolver_para_agente,
    registrar_handoff,
    selecionar_proximo_atendente,
)

# ---------------------------------------------------------------------------
# selecionar_proximo_atendente — round-robin
# ---------------------------------------------------------------------------


def _mock_perfis_query(client: MagicMock, perfis: list[dict]) -> None:
    chain = client.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain = chain.eq.return_value.order.return_value.limit.return_value
    chain.execute.return_value.data = perfis


async def test_selecionar_proximo_atendente_um_atendente_sempre_o_mesmo():
    """Com 1 atendente cadastrado, o round-robin sempre devolve ele."""
    client = MagicMock()
    perfil = {"id": "p1", "nome": "Maria", "role": "atendente", "telefone": "+5511"}
    _mock_perfis_query(client, [perfil])

    p1 = await selecionar_proximo_atendente("tenant-1", "atendente", client)
    p2 = await selecionar_proximo_atendente("tenant-1", "atendente", client)

    assert p1["id"] == "p1"
    assert p2["id"] == "p1"


async def test_selecionar_proximo_atendente_ordena_por_ultimo_handoff_nulls_first():
    """A query deve ordenar por ultimo_handoff_em ASC NULLS FIRST — quem nunca
    atendeu (NULL) entra antes de quem já atendeu."""
    client = MagicMock()
    perfis_ordenados = [
        {"id": "p-nunca-atendeu", "nome": "Ana", "role": "atendente", "telefone": None},
    ]
    _mock_perfis_query(client, perfis_ordenados)

    resultado = await selecionar_proximo_atendente("tenant-1", "atendente", client)

    select_chain = client.table.return_value.select.return_value
    order_call = select_chain.eq.return_value.eq.return_value.eq.return_value.order
    order_call.assert_called_once_with("ultimo_handoff_em", desc=False, nullsfirst=True)
    assert resultado["id"] == "p-nunca-atendeu"


async def test_selecionar_proximo_atendente_retorna_none_se_ninguem_disponivel():
    client = MagicMock()
    _mock_perfis_query(client, [])

    resultado = await selecionar_proximo_atendente("tenant-1", "atendente", client)

    assert resultado is None


async def test_selecionar_proximo_atendente_filtra_por_tenant_role_e_ativo():
    client = MagicMock()
    perfil = {"id": "p1", "nome": "Maria", "role": "concierge", "telefone": None}
    _mock_perfis_query(client, [perfil])

    await selecionar_proximo_atendente("tenant-xyz", "concierge", client)

    select_chain = client.table.return_value.select.return_value
    eq_calls = select_chain.eq.call_args_list
    assert ("tenant_id", "tenant-xyz") in [c.args for c in eq_calls]
    eq2_calls = select_chain.eq.return_value.eq.call_args_list
    assert ("role", "concierge") in [c.args for c in eq2_calls]
    eq3_calls = select_chain.eq.return_value.eq.return_value.eq.call_args_list
    assert ("ativo", True) in [c.args for c in eq3_calls]


# ---------------------------------------------------------------------------
# registrar_handoff
# ---------------------------------------------------------------------------


async def test_registrar_handoff_insere_e_atualiza_perfil():
    client = MagicMock()
    client.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "handoff-1", "status": "aguardando"}
    ]
    perfil = {"id": "perfil-1", "nome": "Maria", "role": "atendente", "telefone": "+5511999990000"}

    resultado = await registrar_handoff(
        tenant_id="tenant-1",
        conversa_id="conversa-1",
        contato_id="contato-1",
        perfil=perfil,
        motivo="Quer agendar consulta",
        resumo_conversa="Paciente João, telefone +5511...",
        vertical_db=client,
    )

    assert resultado["id"] == "handoff-1"

    insert_payload = client.table.return_value.insert.call_args[0][0]
    assert insert_payload["perfil_id"] == "perfil-1"
    assert insert_payload["status"] == "aguardando"
    assert insert_payload["tenant_id"] == "tenant-1"

    update_call = client.table.return_value.update
    update_call.assert_called_once()
    assert "ultimo_handoff_em" in update_call.call_args[0][0]


# ---------------------------------------------------------------------------
# assumir_handoff
# ---------------------------------------------------------------------------


async def test_assumir_handoff_atualiza_status_e_assumido_em():
    client = MagicMock()

    await assumir_handoff("handoff-1", "tenant-1", client)

    update_payload = client.table.return_value.update.call_args[0][0]
    assert update_payload["status"] == "em_atendimento"
    assert "assumido_em" in update_payload
    eq_calls = client.table.return_value.update.return_value.eq.call_args_list
    assert ("id", "handoff-1") in [c.args for c in eq_calls]


# ---------------------------------------------------------------------------
# devolver_para_agente
# ---------------------------------------------------------------------------


async def test_devolver_para_agente_atualiza_core_e_vertical():
    core_db = MagicMock()
    vertical_db = MagicMock()

    await devolver_para_agente("conversa-1", "tenant-1", core_db, vertical_db)

    core_calls = core_db.table.call_args_list
    assert any(c.args[0] == "conversas" for c in core_calls)

    primeiro_update = core_db.table.return_value.update.call_args_list[0].args[0]
    assert primeiro_update["agente_ativo"] is True
    assert primeiro_update["estado"] == "aberta"

    vertical_update = vertical_db.table.return_value.update.call_args[0][0]
    assert vertical_update["status"] == "devolvido"
    assert "encerrado_em" in vertical_update


async def test_devolver_para_agente_nao_propaga_falha_em_colunas_best_effort():
    """status/atribuido_para são melhor esforço — se a coluna não existir
    (erro do PostgREST), devolver_para_agente não deve propagar a exceção."""
    core_db = MagicMock()

    def update_side_effect(payload):
        chain = MagicMock()
        if "status" in payload:
            chain.eq.return_value.eq.return_value.execute.side_effect = Exception(
                "column conversas.status does not exist"
            )
        else:
            chain.eq.return_value.eq.return_value.execute.return_value = MagicMock()
        return chain

    core_db.table.return_value.update.side_effect = update_side_effect
    vertical_db = MagicMock()

    # Não deve levantar exceção mesmo com a 2ª chamada (status/atribuido_para) falhando.
    await devolver_para_agente("conversa-1", "tenant-1", core_db, vertical_db)


# ---------------------------------------------------------------------------
# buscar_handoff_ativo
# ---------------------------------------------------------------------------


async def test_buscar_handoff_ativo_retorna_mais_recente():
    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain = chain.in_.return_value.order.return_value.limit.return_value
    chain.execute.return_value.data = [{"id": "handoff-mais-recente", "status": "em_atendimento"}]

    resultado = await buscar_handoff_ativo("conversa-1", "tenant-1", client)

    assert resultado["id"] == "handoff-mais-recente"


async def test_buscar_handoff_ativo_retorna_none_sem_handoff():
    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain = chain.in_.return_value.order.return_value.limit.return_value
    chain.execute.return_value.data = []

    resultado = await buscar_handoff_ativo("conversa-1", "tenant-1", client)

    assert resultado is None
