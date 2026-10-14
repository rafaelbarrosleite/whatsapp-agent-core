from unittest.mock import MagicMock

from db.privacy import communication_allowed, record_preference

TERM_V2 = {"id": "term-2", "versao": "2", "conteudo_hash": "hash-v2"}


def _vertical(rows_by_purpose: dict[str, list[dict]]) -> MagicMock:
    client = MagicMock()

    def table(name: str):
        assert name == "lgpd_preferencias"
        query = MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query

        def execute():
            purpose = next(
                (call.args[1] for call in query.eq.call_args_list if call.args[0] == "finalidade"),
                "",
            )
            return MagicMock(data=rows_by_purpose.get(purpose, []))

        query.execute.side_effect = execute
        return query

    client.table.side_effect = table
    return client


def _preference(decision: str, *, version: str = "2", term_hash: str = "hash-v2") -> dict:
    return {
        "decisao": decision,
        "termo_versao": version,
        "termo_hash": term_hash,
    }


def test_current_acceptance_allows_only_its_purpose() -> None:
    vertical = _vertical({"followup_comercial": [_preference("aceito")]})
    assert communication_allowed(
        vertical,
        tenant_id="tenant-1",
        contato_id="contact-1",
        channel="whatsapp",
        purpose="followup_comercial",
        current_term=TERM_V2,
    )


def test_revocation_blocks_sending() -> None:
    vertical = _vertical({"followup_comercial": [_preference("revogado")]})
    assert not communication_allowed(
        vertical,
        tenant_id="tenant-1",
        contato_id="contact-1",
        channel="whatsapp",
        purpose="followup_comercial",
        current_term=TERM_V2,
    )


def test_new_term_version_requires_new_acceptance() -> None:
    vertical = _vertical(
        {"atendimento_transacional": [_preference("aceito", version="1", term_hash="hash-v1")]}
    )
    assert not communication_allowed(
        vertical,
        tenant_id="tenant-1",
        contato_id="contact-1",
        channel="whatsapp",
        purpose="atendimento_transacional",
        current_term=TERM_V2,
    )


def test_purpose_opt_out_overrides_transactional_fallback() -> None:
    vertical = _vertical(
        {
            "lembretes": [_preference("revogado")],
            "atendimento_transacional": [_preference("aceito")],
        }
    )
    assert not communication_allowed(
        vertical,
        tenant_id="tenant-1",
        contato_id="contact-1",
        channel="whatsapp",
        purpose="lembretes",
        current_term=TERM_V2,
        transactional_fallback=True,
    )


def test_channel_preference_is_part_of_rpc_record() -> None:
    vertical = MagicMock()
    record_preference(
        vertical,
        tenant_id="tenant-1",
        contato_id="contact-1",
        channel="whatsapp",
        purpose="nps",
        decision="revogado",
        origin="crm",
        term=TERM_V2,
    )
    params = vertical.rpc.call_args.args[1]
    assert params["p_canal"] == "whatsapp"
    assert params["p_finalidade"] == "nps"
    assert params["p_decisao"] == "revogado"
